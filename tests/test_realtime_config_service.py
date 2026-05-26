from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch, MagicMock

from app.services import realtime_config_service


class RealtimeConfigServiceTests(unittest.TestCase):
    def setUp(self):
        realtime_config_service._ice_cache.update({"expires_at": 0.0, "ice_servers": None, "error": ""})

    def test_twilio_credentials_report_healthy_when_token_generation_succeeds(self):
        ice_servers = [{"urls": ["stun:global.stun.twilio.com:3478"]}]
        with patch.multiple(
            realtime_config_service.settings,
            TWILIO_ACCOUNT_SID="AC123",
            TWILIO_AUTH_TOKEN="secret",
        ), patch.object(realtime_config_service, "_fetch_twilio_ice_servers_sync", return_value=ice_servers):
            diagnostics = asyncio.run(realtime_config_service.get_turn_diagnostics(force_refresh=True))
            configured = asyncio.run(realtime_config_service.webrtc_realtime_configured())

        self.assertTrue(diagnostics["configured"])
        self.assertTrue(diagnostics["productionReady"])
        self.assertTrue(diagnostics["udpEnabled"])
        self.assertTrue(diagnostics["tcpEnabled"])
        self.assertTrue(diagnostics["tlsEnabled"])
        self.assertTrue(configured)

    def test_missing_twilio_credentials_are_not_reported_as_configured(self):
        with patch.multiple(
            realtime_config_service.settings,
            TWILIO_ACCOUNT_SID="",
            TWILIO_AUTH_TOKEN="",
        ):
            diagnostics = asyncio.run(realtime_config_service.get_turn_diagnostics(force_refresh=True))
            configured = asyncio.run(realtime_config_service.webrtc_realtime_configured())

        self.assertFalse(diagnostics["configured"])
        self.assertFalse(diagnostics["productionReady"])
        self.assertFalse(configured)
        self.assertTrue(diagnostics["warnings"])
        self.assertTrue(diagnostics["fallback"])

    def test_twilio_failure_returns_stun_fallback_and_not_production_ready(self):
        with patch.multiple(
            realtime_config_service.settings,
            TWILIO_ACCOUNT_SID="AC123",
            TWILIO_AUTH_TOKEN="secret",
        ), patch.object(
            realtime_config_service,
            "_fetch_twilio_ice_servers_sync",
            side_effect=RuntimeError("boom"),
        ):
            diagnostics = asyncio.run(realtime_config_service.get_turn_diagnostics(force_refresh=True))

        self.assertTrue(diagnostics["configured"])
        self.assertFalse(diagnostics["productionReady"])
        self.assertTrue(diagnostics["fallback"])
        self.assertIn("Twilio Network Traversal token generation failed: boom", diagnostics["warnings"])

    def test_twilio_sdk_client_requests_token(self):
        mock_token = MagicMock()
        mock_token.ice_servers = [{"urls": ["stun:global.stun.twilio.com:3478"]}]
        mock_client = MagicMock()
        mock_client.tokens.create.return_value = mock_token

        with patch.multiple(
            realtime_config_service.settings,
            TWILIO_ACCOUNT_SID="AC123",
            TWILIO_AUTH_TOKEN="secret",
        ), patch.object(realtime_config_service, "Client", return_value=mock_client):
            ice_servers = realtime_config_service._fetch_twilio_ice_servers_sync()

        self.assertEqual(ice_servers, mock_token.ice_servers)
        mock_client.tokens.create.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
