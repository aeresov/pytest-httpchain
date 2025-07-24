from http import HTTPStatus

import pytest
from http_server_mock import HttpServerMock

app = HttpServerMock(__name__)


@app.get("/api/v1/users/<int:user_id>")
def get_user(user_id: int):
    return {"id": user_id, "name": f"User {user_id}", "is_premium": user_id < 100, "credits": user_id * 10}, HTTPStatus.OK


@app.post("/api/v1/calculate")
def calculate():
    return {"result": 42, "multiplier": 2}, HTTPStatus.OK


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield {"url": "http://localhost:5000", "port": 5000}
