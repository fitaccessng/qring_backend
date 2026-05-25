from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.base import Base
from app.db.models import CallSession, User, UserRole, VisitorSession
from app.services.visitor_session_auth import issue_visitor_session_token
from app.socket.contracts import RealtimeEvent
from app.socket.manager import socket_state
from app.socket.server import sio

settings = get_settings()


class SocketRealtimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tmpdir.name) / "realtime-test.db"
        self.engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, class_=Session, autoflush=False, autocommit=False)
        self.db = self.SessionLocal()

        self.homeowner = User(
            id=str(uuid.uuid4()),
            full_name="Realtime Homeowner",
            email="realtime-homeowner@example.com",
            password_hash="hashed",
            role=UserRole.homeowner,
            email_verified=True,
        )
        self.db.add(self.homeowner)
        self.db.flush()

        self.session = VisitorSession(
            id=str(uuid.uuid4()),
            qr_id="qr-test",
            home_id=str(uuid.uuid4()),
            door_id=str(uuid.uuid4()),
            homeowner_id=self.homeowner.id,
            visitor_label="Realtime Visitor",
            status="approved",
        )
        self.db.add(self.session)
        self.db.commit()
        self.visitor_token = issue_visitor_session_token(self.db, session=self.session)
        self.homeowner_token = create_access_token(self.homeowner.id, self.homeowner.role.value)

        await socket_state.reset_for_tests()
        self.session_local_patcher = patch("app.socket.events.SessionLocal", self.SessionLocal)
        self.session_local_patcher.start()

        self.emit_calls = []
        self.entered_rooms = []
        self.left_rooms = []

        async def fake_emit(event, payload=None, **kwargs):
            self.emit_calls.append({"event": event, "payload": payload, **kwargs})

        async def fake_enter_room(sid, room, namespace=None):
            self.entered_rooms.append({"sid": sid, "room": room, "namespace": namespace})

        async def fake_leave_room(sid, room, namespace=None):
            self.left_rooms.append({"sid": sid, "room": room, "namespace": namespace})

        self.emit_patcher = patch.object(sio, "emit", fake_emit)
        self.enter_room_patcher = patch.object(sio, "enter_room", fake_enter_room)
        self.leave_room_patcher = patch.object(sio, "leave_room", fake_leave_room)
        self.emit_patcher.start()
        self.enter_room_patcher.start()
        self.leave_room_patcher.start()

        self.handlers = sio.handlers[settings.SIGNALING_NAMESPACE]

    async def asyncTearDown(self):
        self.leave_room_patcher.stop()
        self.enter_room_patcher.stop()
        self.emit_patcher.stop()
        self.session_local_patcher.stop()
        await socket_state.reset_for_tests()
        self.db.close()
        self.engine.dispose()
        self.tmpdir.cleanup()

    def _find_emit(self, event_name: str):
        return [call for call in self.emit_calls if call["event"] == event_name]

    async def _join_homeowner(self, sid: str = "sid-homeowner"):
        connect_handler = self.handlers["connect"]
        join_handler = self.handlers[RealtimeEvent.SESSION_JOIN]
        await connect_handler(sid, {}, {"token": self.homeowner_token})
        return await join_handler(sid, {"sessionId": self.session.id, "displayName": "Homeowner"})

    async def _join_visitor(self, sid: str = "sid-visitor"):
        join_handler = self.handlers[RealtimeEvent.SESSION_JOIN]
        return await join_handler(
            sid,
            {
                "sessionId": self.session.id,
                "displayName": "Visitor",
                "visitorToken": self.visitor_token,
            },
        )

    async def test_join_chat_and_webrtc_signaling_flow(self):
        join_homeowner = await self._join_homeowner()
        join_visitor = await self._join_visitor()
        self.assertTrue(join_homeowner["ok"])
        self.assertTrue(join_visitor["ok"])
        self.assertTrue(any(item["room"] == f"session:{self.session.id}" for item in self.entered_rooms))

        chat_ack = await self.handlers[RealtimeEvent.CHAT_MESSAGE](
            "sid-visitor",
            {
                "sessionId": self.session.id,
                "text": "Hello from visitor",
                "clientId": "msg-1",
                "displayName": "Visitor",
                "senderType": "visitor",
                "visitorToken": self.visitor_token,
            },
        )
        self.assertEqual(chat_ack["status"], "queued")
        self.assertTrue(self._find_emit(RealtimeEvent.CHAT_MESSAGE))
        self.assertTrue(self._find_emit(RealtimeEvent.CHAT_ACK))

        invite_ack = await self.handlers[RealtimeEvent.CALL_INVITE](
            "sid-homeowner",
            {
                "sessionId": self.session.id,
                "callSessionId": str(uuid.uuid4()),
                "hasVideo": True,
                "type": "video",
                "visitorId": self.session.id,
            },
        )
        self.assertTrue(invite_ack["ok"])
        self.assertTrue(self._find_emit(RealtimeEvent.CALL_INVITE))

        accepted_ack = await self.handlers[RealtimeEvent.CALL_ACCEPTED](
            "sid-visitor",
            {
                "sessionId": self.session.id,
                "callSessionId": self._find_emit(RealtimeEvent.CALL_INVITE)[0]["payload"]["callSessionId"],
                "hasVideo": True,
            },
        )
        self.assertTrue(accepted_ack["ok"])
        self.assertTrue(self._find_emit(RealtimeEvent.CALL_ACCEPTED))

        offer_ack = await self.handlers[RealtimeEvent.WEBRTC_OFFER](
            "sid-homeowner",
            {
                "sessionId": self.session.id,
                "callSessionId": self._find_emit(RealtimeEvent.CALL_INVITE)[0]["payload"]["callSessionId"],
                "hasVideo": True,
                "sdp": {"type": "offer", "sdp": "fake-offer"},
            },
        )
        self.assertTrue(offer_ack["ok"])
        self.assertTrue(self._find_emit(RealtimeEvent.WEBRTC_OFFER))

        answer_ack = await self.handlers[RealtimeEvent.WEBRTC_ANSWER](
            "sid-visitor",
            {
                "sessionId": self.session.id,
                "callSessionId": self._find_emit(RealtimeEvent.CALL_INVITE)[0]["payload"]["callSessionId"],
                "sdp": {"type": "answer", "sdp": "fake-answer"},
            },
        )
        self.assertTrue(answer_ack["ok"])
        self.assertTrue(self._find_emit(RealtimeEvent.WEBRTC_ANSWER))

        ice_ack = await self.handlers[RealtimeEvent.WEBRTC_ICE](
            "sid-visitor",
            {
                "sessionId": self.session.id,
                "callSessionId": self._find_emit(RealtimeEvent.CALL_INVITE)[0]["payload"]["callSessionId"],
                "candidate": {
                    "candidate": "candidate:1 1 udp 2122260223 10.0.0.5 54000 typ relay",
                    "sdpMid": "0",
                    "sdpMLineIndex": 0,
                },
            },
        )
        self.assertEqual(ice_ack["candidateType"], "relay")
        self.assertTrue(self._find_emit(RealtimeEvent.WEBRTC_ICE))

    async def test_join_replays_active_ringing_call_after_disconnect_and_rejoin(self):
        active_call = CallSession(
            id=str(uuid.uuid4()),
            visitor_session_id=self.session.id,
            room_name="qring-call-replay-test",
            visitor_id=self.session.id,
            homeowner_id=self.homeowner.id,
            caller_id=self.homeowner.id,
            call_type="audio",
            status="ringing",
        )
        self.db.add(active_call)
        self.db.commit()

        first_join = await self._join_visitor()
        self.assertTrue(first_join["ok"])
        self.assertTrue(self._find_emit(RealtimeEvent.CALL_INVITE))
        self.assertTrue(self._find_emit(RealtimeEvent.SESSION_SNAPSHOT))

        self.emit_calls.clear()
        await self.handlers["disconnect"]("sid-visitor")
        rejoin = await self._join_visitor()
        self.assertTrue(rejoin["ok"])
        self.assertTrue(self._find_emit(RealtimeEvent.CALL_INVITE))
        self.assertTrue(self._find_emit(RealtimeEvent.SESSION_SNAPSHOT))

        diagnostics = await socket_state.diagnostics()
        self.assertGreaterEqual(int(diagnostics["metrics"].get("inviteReplayHits", 0)), 1)


if __name__ == "__main__":
    unittest.main()
