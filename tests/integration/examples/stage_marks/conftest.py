from http import HTTPStatus

import pytest
from http_server_mock import HttpServerMock

app = HttpServerMock(__name__)


@app.get("/success")
def success_endpoint():
    return {"status": "ok"}, HTTPStatus.OK


@app.get("/failure")
def failure_endpoint():
    return {"error": "Not found"}, HTTPStatus.NOT_FOUND


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield
