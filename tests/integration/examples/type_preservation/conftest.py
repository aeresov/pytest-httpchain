from http import HTTPStatus

import pytest
from http_server_mock import HttpServerMock

app = HttpServerMock(__name__)


@app.get("/get_number")
def get_number():
    """Return a JSON response with an integer value."""
    return {"id": 122358, "count": 42}, HTTPStatus.OK


@app.get("/get_another_number")
def get_another_number():
    """Return a JSON response with the same integer value."""
    return {"result": 122358, "value": 42}, HTTPStatus.OK


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield
