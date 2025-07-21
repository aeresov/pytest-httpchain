from http import HTTPStatus

import pytest
from http_server_mock import HttpServerMock

app = HttpServerMock(__name__)


@app.get("/text-content")
def text_content():
    """Return text content for regex testing."""
    return "Hello World! User ID: 12345, Email: user@example.com", HTTPStatus.OK


@app.get("/json-response")
def json_response():
    """Return JSON response for regex testing."""
    return {"message": "Success", "id": 67890, "status": "active"}, HTTPStatus.OK


@app.get("/html-content")
def html_content():
    """Return HTML content for regex testing."""
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>Test Page</title></head>
    <body>
        <h1>Welcome to our site</h1>
        <p>Contact us at support@company.com</p>
        <div class="error">Error code: 404</div>
    </body>
    </html>
    """
    return html, HTTPStatus.OK


@app.get("/error-response")
def error_response():
    """Return error response for testing negative patterns."""
    return {"error": "Invalid request", "code": 400}, HTTPStatus.BAD_REQUEST


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield
