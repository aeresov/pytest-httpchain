import base64
import threading
import time
from http import HTTPStatus

import pytest
from flask import request
from flask_httpauth import HTTPBasicAuth
from http_server_mock import HttpServerMock
from werkzeug.security import check_password_hash, generate_password_hash

app = HttpServerMock(__name__)
auth = HTTPBasicAuth()
users = {"user": generate_password_hash("pass")}

# Thread-safe counter for parallel tests
_counter_lock = threading.Lock()
_counter = 0


def reset_counter():
    global _counter
    with _counter_lock:
        _counter = 0


@auth.verify_password
def verify_password(username, password):
    if username in users and check_password_hash(users[username], password):
        return username


# ============ Basic Endpoints ============


@app.get("/ok")
def ok():
    return {}, HTTPStatus.OK


@app.get("/bad")
def bad():
    return {}, HTTPStatus.BAD_REQUEST


@app.get("/answer")
@auth.login_required
def answer():
    return {"answer": 42}, HTTPStatus.OK


@app.get("/delay/<int:seconds>")
def delay(seconds: int):
    time.sleep(seconds)
    return {"delayed": seconds}, HTTPStatus.OK


# ============ Echo Endpoints (for body type tests) ============


@app.post("/echo/json")
def echo_json():
    """Echo back JSON body"""
    data = request.get_json(force=True, silent=True) or {}
    return {"received": data}, HTTPStatus.OK


@app.post("/echo/form")
def echo_form():
    """Echo back form data"""
    return {"form": dict(request.form)}, HTTPStatus.OK


@app.post("/echo/text")
def echo_text():
    """Echo back text body"""
    return {"text": request.get_data(as_text=True)}, HTTPStatus.OK


@app.post("/echo/xml")
def echo_xml():
    """Echo back XML as text"""
    return {"xml": request.get_data(as_text=True)}, HTTPStatus.OK


@app.post("/echo/binary")
def echo_binary():
    """Echo back binary as base64"""
    data = request.get_data()
    return {"base64": base64.b64encode(data).decode(), "size": len(data)}, HTTPStatus.OK


@app.post("/graphql")
def graphql():
    """Mock GraphQL endpoint"""
    data = request.get_json(force=True, silent=True) or {}
    query = data.get("query", "")
    variables = data.get("variables", {})

    # Simple mock responses based on query content
    if "user" in query.lower():
        user_id = variables.get("id", 1)
        return {"data": {"user": {"id": user_id, "name": f"User {user_id}", "email": f"user{user_id}@example.com"}}}, HTTPStatus.OK
    elif "users" in query.lower():
        return {"data": {"users": [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]}}, HTTPStatus.OK
    else:
        return {"data": None, "errors": [{"message": "Unknown query"}]}, HTTPStatus.OK


# ============ User Endpoints (for save/foreach tests) ============


@app.get("/users")
def get_users():
    """Return list of users"""
    return {
        "users": [
            {"id": 1, "name": "Alice", "role": "admin"},
            {"id": 2, "name": "Bob", "role": "user"},
            {"id": 3, "name": "Charlie", "role": "user"},
        ]
    }, HTTPStatus.OK


@app.get("/user/<int:user_id>")
def get_user(user_id: int):
    """Return single user by ID"""
    users_db = {
        1: {"id": 1, "name": "Alice", "role": "admin", "email": "alice@example.com"},
        2: {"id": 2, "name": "Bob", "role": "user", "email": "bob@example.com"},
        3: {"id": 3, "name": "Charlie", "role": "user", "email": "charlie@example.com"},
    }
    if user_id in users_db:
        return users_db[user_id], HTTPStatus.OK
    return {"error": "User not found"}, HTTPStatus.NOT_FOUND


# ============ Counter Endpoint (for parallel tests) ============


@app.post("/counter")
def increment_counter():
    """Increment and return counter (thread-safe)"""
    global _counter
    with _counter_lock:
        _counter += 1
        return {"count": _counter}, HTTPStatus.OK


# ============ Verification Endpoints ============


@app.get("/headers")
def get_headers():
    """Return custom headers for verification"""
    response_data = {"received_headers": dict(request.headers)}
    # Return response with custom headers
    from flask import make_response

    resp = make_response(response_data)
    resp.headers["X-Custom-Header"] = "test-value"
    resp.headers["X-Request-Id"] = "12345"
    return resp


@app.get("/schema-test")
def schema_test():
    """Return data for JSON schema validation"""
    return {
        "id": 1,
        "name": "Test Item",
        "active": True,
        "tags": ["a", "b", "c"],
        "metadata": {"key": "value"},
    }, HTTPStatus.OK


# ============ Template Test Endpoints ============


@app.get("/template/<value>")
def template_value(value: str):
    """Echo back a template value"""
    return {"value": value}, HTTPStatus.OK


@app.post("/template/compute")
def template_compute():
    """Return computed values for template tests"""
    data = request.get_json(force=True, silent=True) or {}
    return {
        "input": data,
        "computed": {
            "doubled": data.get("number", 0) * 2,
            "upper": data.get("text", "").upper(),
        },
    }, HTTPStatus.OK


# ============ Parametrize Endpoints ============


@app.get("/item/<int:item_id>")
def get_item(item_id: int):
    """Return item by ID for parametrize tests"""
    items = {
        1: {"id": 1, "name": "Item One", "price": 100},
        2: {"id": 2, "name": "Item Two", "price": 200},
        3: {"id": 3, "name": "Item Three", "price": 300},
    }
    if item_id in items:
        return items[item_id], HTTPStatus.OK
    return {"error": "Item not found"}, HTTPStatus.NOT_FOUND


@app.get("/search")
def search():
    """Search with query params for parametrize tests"""
    category = request.args.get("category", "all")
    sort = request.args.get("sort", "name")
    return {"category": category, "sort": sort, "results": []}, HTTPStatus.OK


# ============ Fixtures ============


@pytest.fixture
def server():
    reset_counter()  # Reset counter before each test
    with app.run("localhost", 5000):
        yield "http://localhost:5000"


@pytest.fixture
def api_key():
    """Simple fixture providing an API key"""
    return "test-api-key-12345"


@pytest.fixture
def user_credentials():
    """Fixture providing user credentials"""
    return {"username": "user", "password": "pass"}


@pytest.fixture
def request_id():
    """Factory fixture that generates request IDs"""
    import uuid

    def _make_id():
        return str(uuid.uuid4())

    return _make_id


# ============ User Functions for Tests ============
# Note: auth functions are in auth.py, verify functions are in verify.py, save functions are in save.py
