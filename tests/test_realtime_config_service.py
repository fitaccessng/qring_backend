from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.services import realtime_config_service


class RealtimeConfigServiceTests(unittest.TestCase):
    def test_turn_aliases_are_reported_as_configured(self):
        env = {
            "TURN_URLS": "turn:turn.example.com:3478?transport=udp, turns:turn.example.com:5349?transport=tcp",
            "TURN_USERNAME": "qring-user",
            "TURN_PASSWORD": "super-secret",
        }
        with patch.dict(os.environ, env, clear=False), patch.multiple(
            realtime_config_service.settings,
            WEBRTC_TURN_URL="",
            WEBRTC_TURN_TLS_URL="",
            WEBRTC_TURN_USERNAME="",
            WEBRTC_TURN_CREDENTIAL="",
        ):
            diagnostics = realtime_config_service.get_turn_diagnostics()
            configured = realtime_config_service.webrtc_realtime_configured()

        self.assertTrue(diagnostics["configured"])
        self.assertTrue(diagnostics["udpEnabled"])
        self.assertTrue(diagnostics["tcpEnabled"])
        self.assertTrue(diagnostics["tlsEnabled"])
        self.assertTrue(configured)

    def test_missing_turn_credentials_are_not_reported_as_configured(self):
        env = {
            "TURN_URL": "turn:turn.example.com:3478?transport=udp",
            "TURN_USERNAME": "",
            "TURN_PASSWORD": "",
        }
        with patch.dict(os.environ, env, clear=False), patch.multiple(
            realtime_config_service.settings,
            WEBRTC_TURN_URL="",
            WEBRTC_TURN_TLS_URL="",
            WEBRTC_TURN_USERNAME="",
            WEBRTC_TURN_CREDENTIAL="",
        ):
            diagnostics = realtime_config_service.get_turn_diagnostics()
            configured = realtime_config_service.webrtc_realtime_configured()

        self.assertFalse(diagnostics["configured"])
        self.assertFalse(diagnostics["usernameConfigured"])
        self.assertFalse(diagnostics["credentialConfigured"])
        self.assertFalse(configured)


if __name__ == "__main__":
    unittest.main()
