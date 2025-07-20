from http import HTTPStatus

import pytest
from http_server_mock import HttpServerMock

app = HttpServerMock(__name__)


@app.get("/redirect-source")
def redirect_source():
    """Endpoint that returns a redirect response."""
    return "", HTTPStatus.FOUND, {"Location": "http://localhost:5000/redirect-target"}


@app.get("/redirect-target")
def redirect_target():
    """Target endpoint after redirect."""
    return {"success": True}, HTTPStatus.OK


@app.get("/multiple-redirects-1")
def multiple_redirects_1():
    """First redirect in a chain."""
    return "", HTTPStatus.FOUND, {"Location": "http://localhost:5000/multiple-redirects-2"}


@app.get("/multiple-redirects-2")
def multiple_redirects_2():
    """Second redirect in a chain."""
    return "", HTTPStatus.FOUND, {"Location": "http://localhost:5000/redirect-target"}


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield
