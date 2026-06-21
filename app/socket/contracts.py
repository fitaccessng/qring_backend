from __future__ import annotations


class RealtimeEvent:
    SESSION_JOIN = "session.join"
    SESSION_JOINED = "session.joined"
    SESSION_JOIN_DENIED = "session.join_denied"
    SESSION_LEAVE = "session.leave"
    SESSION_PARTICIPANT_JOINED = "session.participant_joined"
    SESSION_PARTICIPANT_LEFT = "session.participant_left"
    SESSION_SNAPSHOT = "session.snapshot"
    SESSION_PRESENCE = "session.presence"
    SESSION_CONTROL = "session.control"
    SESSION_STATUS = "session.status"
    SESSION_ACTIVATED = "session.activated"

    CHAT_MESSAGE = "chat.message"
    CHAT_ACK = "chat.ack"
    CHAT_PERSISTED = "chat.persisted"
    CHAT_PERSIST_FAILED = "chat.persist_failed"
    CHAT_TYPING = "chat.typing"
    CHAT_READ = "chat.read"

    CALL_REQUESTED = "call.requested"
    CALL_INVITE = "call.invite"
    CALL_INVITE_RECEIVED = "call.invite.received"
    CALL_ACCEPTED = "call.accepted"
    CALL_REJECTED = "call.rejected"
    CALL_ENDED = "call.ended"
    CALL_FAILED = "call.failed"

    WEBRTC_OFFER = "webrtc.offer"
    WEBRTC_ANSWER = "webrtc.answer"
    WEBRTC_ICE = "webrtc.ice"
    WEBRTC_ICE_CANDIDATE = "webrtc.ice_candidate"
