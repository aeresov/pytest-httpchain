from http import HTTPStatus

import pytest
from http_server_mock import HttpServerMock


@pytest.fixture
def string_value():
    return "answer"


@pytest.fixture
def number_value():
    return 42


@pytest.fixture
def float_value():
    return 3.14


@pytest.fixture
def dict_value(string_value, number_value):
    return {"string": string_value, "number": number_value}


app = HttpServerMock(__name__)


@app.get("/answer")
def answer():
    return {"answer": 42}, HTTPStatus.OK


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield "http://localhost:5000"
