from http import HTTPStatus

import pytest
from http_server_mock import HttpServerMock

app = HttpServerMock(__name__)


@app.get("/endpoint1")
def endpoint1():
    return {
        "Status": {
            "Result": {
                "List": [
                    {"Id": "test1_value"},
                    {"Id": "test2_value"},
                    {"Id": "test3_value"},
                    {"Id": "test4_value"},
                ]
            }
        }
    }, HTTPStatus.OK


@app.get("/endpoint2")
def endpoint2():
    return {
        "Status": {
            "Result": {
                "List": [
                    {"Id": "test1_value"},
                    {"Id": "test6_value"},
                    {"Id": "test7_value"},
                    {"Id": "test8_value"},
                ]
            }
        }
    }, HTTPStatus.OK


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield
