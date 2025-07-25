import socket
from http import HTTPStatus

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


# Example of creating custom app instances
custom_app1 = HttpServerMock(__name__ + "_custom1")
custom_app2 = HttpServerMock(__name__ + "_custom2")


@custom_app1.get("/app1")
def app1_endpoint():
    return {"app": "custom1"}, HTTPStatus.OK


@custom_app2.post("/app2")
def app2_endpoint():
    return {"app": "custom2"}, HTTPStatus.CREATED


@pytest.fixture
def custom_server1(server_factory):
    """Example of using server_factory with a custom app instance."""
    # Let server_factory find an available port
    with server_factory(custom_app1) as url:
        yield url


@pytest.fixture
def custom_server2(server_factory):
    """Example of using server_factory with another custom app instance."""
    # Let server_factory find an available port
    with server_factory(custom_app2) as url:
        yield url


@pytest.fixture
def multi_server(server_factory):
    """Example of running multiple servers simultaneously."""
    # First server gets an automatic port
    with server_factory(custom_app1) as url1:
        # Second server also gets an automatic port (will skip first port since it's in use)
        with server_factory(custom_app2) as url2:
            yield {
                "server1": url1,
                "server2": url2,
            }
