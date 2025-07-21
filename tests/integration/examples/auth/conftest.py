from http import HTTPStatus

import pytest
from http_server_mock import HttpServerMock

app = HttpServerMock(__name__)


@app.get("/api/public")
def public_endpoint():
    return {"message": "public data"}, HTTPStatus.OK


@app.get("/api/protected")
def protected_endpoint():
    from flask import request

    auth_header = request.headers.get("Authorization", "")
    if auth_header == "Basic dXNlcjpwYXNz":  # user:pass in base64
        return {"message": "protected data", "user": "user"}, HTTPStatus.OK
    else:
        return {"error": "Unauthorized"}, HTTPStatus.UNAUTHORIZED


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield
