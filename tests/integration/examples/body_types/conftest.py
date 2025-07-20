from http import HTTPStatus

import pytest
from http_server_mock import HttpServerMock

app = HttpServerMock(__name__)


@app.post("/echo")
def echo_post():
    """Echo back the request details for testing different body types."""
    from flask import request

    response_data = {
        "method": request.method,
        "content_type": request.content_type,
        "headers": dict(request.headers),
    }

    # Handle different content types
    if request.is_json:
        response_data["body_type"] = "json"
        response_data["json"] = request.get_json()
    elif request.content_type == "application/x-www-form-urlencoded":
        response_data["body_type"] = "form"
        response_data["form"] = dict(request.form)
    elif request.content_type == "application/xml":
        response_data["body_type"] = "xml"
        response_data["xml"] = request.get_data(as_text=True)
    elif request.content_type and request.content_type.startswith("multipart/form-data"):
        response_data["body_type"] = "files"
        response_data["files"] = {}
        for field_name, file_obj in request.files.items():
            response_data["files"][field_name] = file_obj.read().decode("utf-8")
    else:
        response_data["body_type"] = "raw"
        response_data["text"] = request.get_data(as_text=True)

    return response_data, HTTPStatus.OK


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield
