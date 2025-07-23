from http import HTTPStatus

import pytest
from http_server_mock import HttpServerMock

app = HttpServerMock(__name__)


@app.get("/echo/<value>")
def echo_value(value):
    return {"echo": value}, HTTPStatus.OK


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield
