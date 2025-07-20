import time
from http import HTTPStatus

import pytest
from http_server_mock import HttpServerMock

app = HttpServerMock(__name__)


@app.get("/slow")
def slow():
    time.sleep(2)  # Sleep for 2 seconds
    return {"status": "complete"}, HTTPStatus.OK


@app.get("/fast")
def fast():
    return {"status": "ok"}, HTTPStatus.OK


@app.get("/quick")
def quick():
    return {"message": "fast response"}, HTTPStatus.OK


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield
