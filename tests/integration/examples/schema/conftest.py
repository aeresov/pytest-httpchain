from http import HTTPStatus

import pytest
from http_server_mock import HttpServerMock

app = HttpServerMock(__name__)


@app.get("/user/valid")
def valid_user():
    return {
        "id": 123,
        "name": "John Doe",
        "email": "john@example.com",
        "age": 30,
    }, HTTPStatus.OK


@app.get("/user/invalid")
def invalid_user():
    return {
        "id": "not-a-number",  # Schema expects integer
        "name": "Jane Doe",
        # Missing required "email" field
        "age": "thirty",  # Schema expects integer
    }, HTTPStatus.OK


@app.get("/user/extra-fields")
def user_with_extras():
    return {
        "id": 456,
        "name": "Bob Smith",
        "email": "bob@example.com",
        "age": 25,
        "extra_field": "not in schema",
    }, HTTPStatus.OK


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield
