import pytest
from http_server_mock import HttpServerMock

app = HttpServerMock(__name__)


@app.get("/status/<int:code>")
def status_endpoint(code):
    """Return any status code requested."""
    return {"status": code}, code


@pytest.fixture
def server():
    with app.run("localhost", 5001):
        yield "http://localhost:5001"
