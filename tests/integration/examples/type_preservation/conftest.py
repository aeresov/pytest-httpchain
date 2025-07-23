from http import HTTPStatus

import pytest
from http_server_mock import HttpServerMock

app = HttpServerMock(__name__)


@app.get("/echo_params")
def echo_params():
    """Echo back the query parameters to verify their types."""
    from flask import request

    # Get all query parameters
    params = {}
    for key, value in request.args.items():
        params[key] = {"value": value, "type": type(value).__name__}

    return params, HTTPStatus.OK


@app.post("/save_values")
def save_values():
    """Return different types of values for saving."""
    return {"int_value": 42, "float_value": 3.14, "bool_value": True, "string_value": "hello", "list_value": [1, 2, 3], "dict_value": {"nested": "object"}}, HTTPStatus.OK


@app.post("/echo_json")
def echo_json():
    """Echo back the JSON body to verify types are preserved."""
    from flask import request

    # Return the exact JSON that was received
    return request.get_json(), HTTPStatus.OK


@pytest.fixture
def server():
    with app.run("localhost", 5000):
        yield
