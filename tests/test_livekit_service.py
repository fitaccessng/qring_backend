from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services import livekit_service


class LivekitServiceUrlTests(unittest.TestCase):
    def test_build_livekit_client_url_preserves_wss(self):
        with patch.object(livekit_service.settings, "LIVEKIT_URL", "wss://qring-yovnizqn.livekit.cloud"):
            self.assertEqual(
                livekit_service.build_livekit_client_url(),
                "wss://qring-yovnizqn.livekit.cloud",
            )

    def test_build_livekit_server_url_converts_wss_to_https(self):
        with patch.object(livekit_service.settings, "LIVEKIT_URL", "wss://qring-yovnizqn.livekit.cloud"):
            self.assertEqual(
                livekit_service.build_livekit_server_url(),
                "https://qring-yovnizqn.livekit.cloud",
            )

    def test_build_livekit_client_url_converts_https_to_wss(self):
        with patch.object(livekit_service.settings, "LIVEKIT_URL", "https://qring-yovnizqn.livekit.cloud"):
            self.assertEqual(
                livekit_service.build_livekit_client_url(),
                "wss://qring-yovnizqn.livekit.cloud",
            )


if __name__ == "__main__":
    unittest.main()
