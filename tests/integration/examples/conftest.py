import base64
import os
import secrets
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from http import HTTPStatus
from urllib.parse import unquote_to_bytes

import msgpack
import pytest
from flask import Flask, Response, request
from flask_httpauth import HTTPBasicAuth, HTTPDigestAuth, HTTPTokenAuth
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.serving import make_server

app = Flask(__name__)
auth = HTTPBasicAuth()
users = {"user": generate_password_hash("pass")}

# Digest keeps the plain password: the server computes the same digest.
digest_auth = HTTPDigestAuth(realm="examples")
DIGEST_OPAQUE = "examples-opaque"
# The nonces issued, kept here instead of in Flask's session (the default),
# which needs a secret key and the session cookie back with every answer.
# Module-level like the counter: a scenario's stages each get a server of
# their own, and the scenario's digest auth answers them all with one nonce.
_digest_nonces: set[str] = set()

# Tokens /login issued, for /me. "s3cret-" marks them, so a test can tell
# whether one got out.
token_auth = HTTPTokenAuth(scheme="Bearer")
_tokens: set[str] = set()

# Thread-safe counter for parallel tests
_counter_lock = threading.Lock()
_counter = 0

# Requests that reached /flaky (see there).
_flaky_lock = threading.Lock()
_flaky_calls = 0

# Requests that reached /barrier (see there).
_barrier = threading.Condition()
_arrived = 0

# What POST /resources created, by id, and the last id it gave. Module-level
# like the counter: a scenario creating resources in one stage and deleting
# them in another is served by one `api_root` server for all its stages.
_resources_lock = threading.Lock()
_resources: dict[int, dict] = {}
_last_resource_id = 0

# What POST /jobs started, by id: how many polls each needs to be done and how
# many it has had. Module-level like the resources, for the same reason.
_jobs_lock = threading.Lock()
_jobs: dict[int, dict] = {}


def reset_server_state():
    global _counter, _flaky_calls, _arrived, _last_resource_id
    with _counter_lock:
        _counter = 0
    with _flaky_lock:
        _flaky_calls = 0
    with _barrier:
        _arrived = 0
    with _resources_lock:
        _resources.clear()
        _last_resource_id = 0
    with _jobs_lock:
        _jobs.clear()


@auth.verify_password
def verify_password(username, password):
    if username in users and check_password_hash(users[username], password):
        return username


@digest_auth.get_password
def digest_password(username):
    return {"user": "pass"}.get(username)


@digest_auth.generate_nonce
def generate_nonce():
    nonce = secrets.token_hex(16)
    _digest_nonces.add(nonce)
    return nonce


@digest_auth.verify_nonce
def verify_nonce(nonce):
    return nonce in _digest_nonces


@digest_auth.generate_opaque
def generate_opaque():
    return DIGEST_OPAQUE


@digest_auth.verify_opaque
def verify_opaque(opaque):
    return opaque == DIGEST_OPAQUE


@token_auth.verify_token
def verify_token(token):
    if token in _tokens:
        return "user"


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


@app.route("/digest", methods=["GET", "POST"])
@digest_auth.login_required
def digest_protected():
    return {"user": digest_auth.current_user()}, HTTPStatus.OK


@app.post("/login")
def login():
    """A bearer token for the JSON body's username and password, for /me."""
    data = request.get_json(force=True, silent=True) or {}
    if not verify_password(data.get("username"), data.get("password", "")):
        return {"error": "invalid credentials"}, HTTPStatus.UNAUTHORIZED
    token = f"s3cret-{secrets.token_hex(8)}"
    _tokens.add(token)
    return {"token": token}, HTTPStatus.OK


@app.get("/me")
@token_auth.login_required
def me():
    return {"user": token_auth.current_user()}, HTTPStatus.OK


@app.get("/delay/<int:seconds>")
def delay(seconds: int):
    time.sleep(seconds)
    return {"delayed": seconds}, HTTPStatus.OK


@app.get("/delay_ms/<int:ms>")
def delay_ms(ms: int):
    # Millisecond-granularity delay for client-timeout tests: /delay_ms/600
    # with a 0.1s client timeout keeps a comfortable margin without paying
    # whole seconds of sleep per run.
    time.sleep(ms / 1000)
    return {"delayed_ms": ms}, HTTPStatus.OK


@app.get("/barrier/<int:parties>")
def barrier(parties: int):
    """Answer once ``parties`` requests are in flight together, or 504 after
    ``?timeout=`` seconds (5 by default): anything capping concurrency below
    ``parties``, such as a connection pool, keeps them from ever meeting. A
    concurrency test built on it passes in no more time than the requests take
    to arrive, whatever the machine's speed, and fails with a status."""
    global _arrived
    with _barrier:
        _arrived += 1
        _barrier.notify_all()
        met = _barrier.wait_for(lambda: _arrived >= parties, timeout=float(request.args.get("timeout", 5)))
    return {"arrived": _arrived}, HTTPStatus.OK if met else HTTPStatus.GATEWAY_TIMEOUT


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


@app.post("/echo/msgpack")
def echo_msgpack():
    packet = msgpack.unpackb(request.get_data(), raw=False)
    content_type = "application/octet-stream" if request.args.get("wrong_type") else "application/msgpack"
    return Response(msgpack.packb(packet, use_bin_type=True), content_type=content_type)


@app.get("/echo/msgpack-param")
def echo_msgpack_param():
    encoded = request.query_string.partition(b"=")[2].replace(b"+", b" ")
    packet = msgpack.unpackb(unquote_to_bytes(encoded), raw=False)
    return Response(msgpack.packb(packet, use_bin_type=True), content_type="application/x-msgpack")


@app.post("/echo/multipart")
def echo_multipart():
    """Echo a multipart body. `form`: each form field's values, in the order
    sent. `files`: each file part per name, in the order sent, with its
    filename, content type, size and content as text (undecodable bytes
    replaced). `fields`: the first file of each name, filename and size."""
    files: dict[str, list[dict]] = {}
    for name, f in request.files.items(multi=True):
        data = f.read()
        files.setdefault(name, []).append({"filename": f.filename, "content_type": f.content_type, "size": len(data), "text": data.decode(errors="replace")})
    fields = {name: {"filename": parts[0]["filename"], "size": parts[0]["size"]} for name, parts in files.items()}
    return {"fields": fields, "form": request.form.to_dict(flat=False), "files": files}, HTTPStatus.OK


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


@app.get("/flaky/<int:every>")
def flaky(every: int):
    """500 for every ``every``-th request since the server state was reset,
    200 for the others: an endpoint failing a known share of a parallel
    stage's requests, in whatever order they arrive."""
    global _flaky_calls
    with _flaky_lock:
        _flaky_calls += 1
        call = _flaky_calls
    return {"call": call}, HTTPStatus.INTERNAL_SERVER_ERROR if call % every == 0 else HTTPStatus.OK


# ============ Resource Endpoints (for parallel.collect_saves tests) ============


@app.post("/resources")
def create_resource():
    """Create a resource from the JSON body: 201 with it and the id it was given."""
    global _last_resource_id
    data = request.get_json(force=True, silent=True) or {}
    with _resources_lock:
        _last_resource_id += 1
        resource = {**data, "id": _last_resource_id}
        _resources[_last_resource_id] = resource
    return resource, HTTPStatus.CREATED


@app.get("/resources")
def list_resources():
    """Every resource created and not deleted yet, in the order they were created."""
    with _resources_lock:
        return {"resources": list(_resources.values())}, HTTPStatus.OK


@app.delete("/resources/<int:resource_id>")
def delete_resource(resource_id: int):
    with _resources_lock:
        if _resources.pop(resource_id, None) is None:
            return {"error": "Resource not found"}, HTTPStatus.NOT_FOUND
    return "", HTTPStatus.NO_CONTENT


# ============ Job Endpoints (for retry tests) ============


@app.post("/jobs")
def create_job():
    """Start a job, done once polled as many times as the JSON body's ``polls``
    says (3 by default): 202 with its id."""
    data = request.get_json(force=True, silent=True) or {}
    with _jobs_lock:
        job_id = len(_jobs) + 1
        _jobs[job_id] = {"polls": int(data.get("polls", 3)), "polled": 0}
    return {"id": job_id, "status": "pending"}, HTTPStatus.ACCEPTED


@app.get("/jobs/<int:job_id>")
def poll_job(job_id: int):
    """One poll of a job: ``pending`` until its last, ``done`` with its
    ``result`` from then on. ``polled`` counts the polls, this one included."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return {"error": "Job not found"}, HTTPStatus.NOT_FOUND
        job["polled"] += 1
        polled, done = job["polled"], job["polled"] >= job["polls"]
    body = {"id": job_id, "status": "done" if done else "pending", "polled": polled}
    return {**body, "result": f"report-{job_id}"} if done else body, HTTPStatus.OK


# ============ Redirect Endpoints ============


@app.get("/redirect-ok")
def redirect_ok():
    """302 to /ok, for redirect-following tests"""
    from flask import redirect

    return redirect("/ok")


@app.get("/redirect-bad")
def redirect_bad():
    """302 to /bad, for redirect report-labeling tests"""
    from flask import redirect

    return redirect("/bad")


@app.post("/redirect-post/<int:code>")
def redirect_post(code: int):
    """POST redirected with `code` to the ?to= path, for redirect body tests:
    a 307/308 re-sends the body, a 302 turns the request into a bodiless GET"""
    from flask import redirect

    return redirect(request.args["to"], code=code)


@app.get("/template-literal")
def template_literal_body():
    """Server data that LOOKS like a template expression: the engine must save
    it literally, never evaluate it (response data is not scenario code)."""
    return {"tpl": "literal {{ probe }} text"}, HTTPStatus.OK


@app.get("/page")
def html_page():
    """An HTML page, not JSON, for regex saves: a form's CSRF token, an order
    number in running text, and a list of links."""
    page = (
        "<html><body>\n"
        '<form action="/echo/form" method="post"><input type="hidden" name="csrf" value="c5rf-t0ken"></form>\n'
        "<p>Order #1042 is confirmed.</p>\n"
        '<ul><li><a href="/item/1">One</a></li><li><a href="/item/2">Two</a></li><li><a href="/item/3">Three</a></li></ul>\n'
        "</body></html>\n"
    )
    return page, HTTPStatus.OK, {"Content-Type": "text/html; charset=utf-8"}


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


@app.get("/scoped-cookies")
def scoped_cookies():
    """Return two valid same-name cookies with different path scopes."""
    from flask import make_response

    resp = make_response({})
    resp.set_cookie("session", "root", path="/")
    resp.set_cookie("session", "admin", path="/admin")
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


def _free_port() -> int:
    """Ask the OS for an unused TCP port, then release it for the mock server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def closed_port():
    """A port that was just bound and released — connecting to it is REFUSED
    on every platform. A static port cannot promise that portably: a low port
    is silently DROPPED by the Windows CI runners' firewall (a timeout, not a
    refusal), and a high port may sit inside an OS's ephemeral range and be
    legitimately in use."""
    return _free_port()


@contextmanager
def _run_app(ssl_context: ssl.SSLContext | None = None):
    """Serve the Flask app on an OS-assigned port in a background thread.

    werkzeug's ``make_server`` binds port 0 directly (no bind-then-release
    race), and its threaded server runs handlers on daemon threads — teardown
    returns immediately even while a ``/delay`` handler is still sleeping,
    instead of blocking until the sleep finishes.

    With an ``ssl_context`` the same app is served over real TLS and the yielded
    URL carries the ``https`` scheme.
    """
    http_server = make_server("127.0.0.1", 0, app, threaded=True, ssl_context=ssl_context)
    thread = threading.Thread(target=http_server.serve_forever, name="examples-http-server")
    thread.start()
    try:
        scheme = "https" if ssl_context is not None else "http"
        yield f"{scheme}://127.0.0.1:{http_server.server_port}"
    finally:
        http_server.shutdown()
        thread.join()
        http_server.server_close()


@pytest.fixture
def server():
    reset_server_state()  # Before each test
    with _run_app() as url:
        yield url


# The environment variables `api_root` and `https_proxy` export their URL in.
API_ROOT_ENV = "HTTPCHAIN_EXAMPLE_API_ROOT"
HTTPS_PROXY_ENV = "HTTPCHAIN_EXAMPLE_HTTPS_PROXY"


@contextmanager
def _exported(name: str, value: str):
    """``value`` in the environment variable ``name`` for the block."""
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield value
    finally:
        if previous is None:
            del os.environ[name]
        else:
            os.environ[name] = previous


@pytest.fixture(scope="class")
def api_root():
    """The app served for a whole scenario, its URL exported as
    ``HTTPCHAIN_EXAMPLE_API_ROOT``.

    A scenario's ``client`` block resolves once, against scenario substitutions
    only, never a fixture (HTTPCHAIN016), so a base URL comes from the scenario
    or from the environment, as a real suite's would:
    ``"base_url": "{{ env('HTTPCHAIN_EXAMPLE_API_ROOT') }}"``. A scenario asks
    for this with ``usefixtures('api_root')`` in its marks, which sets the
    variable up before its first stage builds the client; class scope serves
    all its stages from the one server.
    """
    reset_server_state()
    with _run_app() as url, _exported(API_ROOT_ENV, url):
        yield url


# Scenario-level ``ssl`` is resolved once, before any fixture value can reach a
# template (a scenario-level template referencing a fixture is HTTPCHAIN017), so
# a scenario cannot interpolate a fixture-provided certificate path. The TLS
# fixtures below instead drop their throwaway PEMs under these fixed names in
# the CWD — which under pytester is the scenario file's own directory — and
# scenarios name them as scenario-relative ``ssl.verify`` / ``ssl.cert`` paths.
HTTPS_CA_BUNDLE = "ca.pem"
HTTPS_CLIENT_BUNDLE = "client.pem"


def _tls_server_context(ca, *, require_client_cert: bool) -> ssl.SSLContext:
    """A server context holding a cert for 127.0.0.1 issued by ``ca``, with
    ``ca``'s bundle written to HTTPS_CA_BUNDLE for the scenario to trust."""
    ca.cert_pem.write_to_path(HTTPS_CA_BUNDLE)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with ca.issue_cert("127.0.0.1").private_key_and_cert_chain_pem.tempfile() as pem:
        context.load_cert_chain(pem)
    if require_client_cert:
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=HTTPS_CA_BUNDLE)
    return context


@pytest.fixture
def https_server():
    """The app over real TLS, with the issuing CA's bundle at ``ca.pem``.

    The SSL unit tests assert what the engine hands ``httpx.Client``; this is
    the other half — that a scenario's ``ssl.verify`` actually decides whether a
    genuine handshake against this certificate succeeds. `trustme` is imported
    lazily: every integration test copies this conftest, and pulling in
    `cryptography` on each of those runs costs more than the TLS tests save.
    """
    import trustme

    with _run_app(_tls_server_context(trustme.CA(), require_client_cert=False)) as url:
        yield url


@pytest.fixture
def mtls_server():
    """Like ``https_server``, but the server also demands a client certificate.

    The client bundle (key + chain, the single-file ``ssl.cert`` form) lands at
    ``client.pem``. A scenario that trusts ``ca.pem`` but sends no client
    certificate fails the handshake, which is what makes the positive case
    evidence that ``ssl.cert`` was really presented.
    """
    import trustme

    ca = trustme.CA()
    context = _tls_server_context(ca, require_client_cert=True)
    ca.issue_cert("client@example.com").private_key_and_cert_chain_pem.write_to_path(HTTPS_CLIENT_BUNDLE)

    with _run_app(context) as url:
        yield url


@pytest.fixture(scope="class")
def https_proxy():
    """The app over TLS as the scenario's proxy, its URL exported as
    ``HTTPCHAIN_EXAMPLE_HTTPS_PROXY``, like ``api_root``'s.

    It demands a client certificate, so a request through it that succeeds
    shows both halves of ``ssl`` reached the proxy's own TLS connection: the
    CA bundle at ``ca.pem`` (or ``verify: false``) and the certificate at
    ``client.pem``. The app answers a proxy's absolute-form request as its
    own, whatever host it names.
    """
    import trustme

    ca = trustme.CA()
    context = _tls_server_context(ca, require_client_cert=True)
    ca.issue_cert("client@example.com").private_key_and_cert_chain_pem.write_to_path(HTTPS_CLIENT_BUNDLE)

    with _run_app(context) as url, _exported(HTTPS_PROXY_ENV, url):
        yield url


@pytest.fixture
def server_keep():
    """Like ``server`` but does NOT reset the shared counter at setup.

    The counter is a process-global, so a later stage using ``server_keep`` can
    observe the running total accumulated by an earlier stage that used
    ``server`` — even though each stage gets its own function-scoped fixture and
    its own ephemeral port (M50).
    """
    with _run_app() as url:
        yield url


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


@pytest.fixture(scope="class")
def connection():
    """A class-scoped resource, closed once the scenario's last stage is done."""
    conn = {"closed": False}
    yield conn
    conn["closed"] = True


@pytest.fixture
def transaction(connection):
    """Factory fixture whose value is a context manager built on ``connection``:
    ``{{ transaction('t1') }}`` begins one, and its exit commits it, printing
    so, or raises — on a closed connection, as a real commit would, or when
    asked to with ``fail=True``."""

    @contextmanager
    def _transaction(name, fail=False):
        yield name
        if connection["closed"]:
            raise RuntimeError(f"cannot commit {name}: connection closed")
        if fail:
            raise RuntimeError(f"commit of {name} rejected")
        print(f"transaction {name} committed")

    return _transaction


@pytest.fixture(scope="class", params=["alpha", "beta"])
def tenant(request):
    """A class-scoped fixture with params: a scenario whose every stage requests
    it runs its whole chain once per tenant. Each setup is printed, so a test
    can count them."""
    print(f"tenant setup: {request.param}")
    return request.param


@pytest.fixture
def beta_setup_error(tenant):
    """Fail setup for the ``beta`` tenant only: the first stage of a chain that
    is not the scenario's first."""
    if tenant == "beta":
        raise RuntimeError("fixture setup failed for beta")


@pytest.fixture
def fixture_setup_error():
    """Fail before a generated stage method can enter Carrier.execute_stage."""
    raise RuntimeError("fixture setup failed")


@pytest.fixture
def fixture_teardown_error():
    """Fail after the stage body passed, before pytest advances the chain."""
    yield
    raise RuntimeError("fixture teardown failed")


# ============ Error Testing Endpoints ============


@app.get("/malformed-json")
def malformed_json():
    """Return invalid JSON for error handling tests"""
    from flask import Response

    return Response("{invalid json", mimetype="application/json", status=200)


# ============ User Functions for Tests ============
# Note: auth functions are in auth.py, verify functions are in verify.py, save functions are in save.py
