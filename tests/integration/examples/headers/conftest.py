from http import HTTPStatus

import pytest
from http_server_mock import HttpServerMock

app = HttpServerMock(__name__)


@app.get("/headers")
def headers_endpoint():
    """Return a response with specific headers."""
    headers = {
        "Content-Type": "application/json",
        "X-Custom-Header": "custom-value",
        "Cache-Control": "no-cache",
        "X-API-Version": "1.0"
    }
    return {"message": "success"}, HTTPStatus.OK, headers


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield
