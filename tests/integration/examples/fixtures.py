import contextlib
import socket

import pytest
from http_server_mock import HttpServerMock


def find_available_port(start_port=5000):
    """Find the next available port starting from start_port."""
    port = start_port
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("localhost", port))
                return port
            except OSError:
                port += 1


@pytest.fixture
def server_factory():
    """Factory fixture that takes an HttpServerMock instance and starts it."""

    @contextlib.contextmanager
    def _server_factory(mock_app: HttpServerMock, port: int | None = None):
        if port is None:
            port = find_available_port()

        with mock_app.run("localhost", port):
            yield f"http://localhost:{port}"

    return _server_factory
