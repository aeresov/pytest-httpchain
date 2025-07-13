from http import HTTPMethod, HTTPStatus

import pytest
from http_server_mock import HttpServerMock


@pytest.fixture
def string_value():
    return "test_value"


@pytest.fixture
def number_value():
    return 123


@pytest.fixture
def dict_value():
    return {"key": "value", "number": 42}


app = HttpServerMock(__name__)


@app.route("/ok", methods=[HTTPMethod.GET])
def ok():
    return {}, HTTPStatus.OK


@app.route("/bad", methods=[HTTPMethod.GET])
def bad():
    return {}, HTTPStatus.BAD_REQUEST


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield
