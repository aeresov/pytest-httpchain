from http import HTTPStatus

import pytest
from flask_httpauth import HTTPBasicAuth
from http_server_mock import HttpServerMock
from werkzeug.security import check_password_hash, generate_password_hash


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
auth = HTTPBasicAuth()

users = {"user": generate_password_hash("pass")}


@auth.verify_password
def verify_password(username, password):
    if username in users and check_password_hash(users.get(username), password):
        return username


@app.get("/answer")
@auth.login_required
def answer():
    return {"answer": 42}, HTTPStatus.OK


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield "http://localhost:5000"
