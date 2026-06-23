from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.socket.server import create_socketio_manager


class SocketRedisAdapterTests(unittest.TestCase):
    def test_create_socketio_manager_returns_redis_manager_when_url_is_set(self):
        mock_manager = MagicMock(name="AsyncRedisManager")
        with patch("app.socket.server.socketio.AsyncRedisManager", return_value=mock_manager) as factory:
            manager = create_socketio_manager("redis://localhost:6379/0", "qring-socketio")

        self.assertIs(manager, mock_manager)
        factory.assert_called_once_with("redis://localhost:6379/0", channel="qring-socketio", write_only=False)

    def test_create_socketio_manager_returns_none_without_redis_url(self):
        with patch("app.socket.server.socketio.AsyncRedisManager") as factory:
            manager = create_socketio_manager("", "qring-socketio")

        self.assertIsNone(manager)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
