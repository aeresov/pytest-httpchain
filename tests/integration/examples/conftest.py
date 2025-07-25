import time
from http import HTTPStatus

import pytest
from fixtures import server_factory  # noqa
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


@app.get("/ok")
def ok():
    return {}, HTTPStatus.OK


@app.get("/bad")
def bad():
    return {}, HTTPStatus.BAD_REQUEST


@app.get("/path_param_number/{number_param}")
def path_param_number(number_param: int):
    return {"answer": {"param": number_param + 1}}, HTTPStatus.OK


@app.get("/path_param_string/{string_param}")
def path_param_string(string_param: str):
    return {"answer": {"param": string_param}}, HTTPStatus.OK


@app.get("/delay/{seconds}")
def delay(seconds: int):
    time.sleep(seconds)
    return {"delayed": seconds}, HTTPStatus.OK


@pytest.fixture
def server(server_factory):
    with server_factory(app) as url:
        yield url
