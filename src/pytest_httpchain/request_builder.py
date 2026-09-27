"""Translation of resolved scenario models into httpx call arguments: the
client's, once per scenario, and a request's, once per iteration.

Both builders take already-resolved models (walking templates is the carrier's
job) and the scenario's directory, which relative paths resolve against.
"""

import base64
import os
import re
import ssl
import threading
from collections.abc import Generator, Iterable, Mapping
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus

import httpx

from pytest_httpchain.errors import RequestError, StageExecutionError
from pytest_httpchain.models import (
    Auth,
    Base64Body,
    BasicAuth,
    BearerAuth,
    BinaryBody,
    ClientConfig,
    DigestAuth,
    FilesBody,
    FormBody,
    GraphQLBody,
    JsonBody,
    Request,
    RequestAuth,
    SSLConfig,
    TextBody,
    UserFunctionKwargs,
    UserFunctionName,
    XmlBody,
    is_relative_url,
)
from pytest_httpchain.redaction import DEFAULT_REDACTION, Redaction
from pytest_httpchain.templates import TEMPLATE_PATTERN, contains_template
from pytest_httpchain.userfunc import UserFunctionError, call_target, call_user_function, import_function
from pytest_httpchain.utils import resolve_scenario_path


def build_ssl_verify(config: SSLConfig, scenario_dir: Path | None) -> bool | ssl.SSLContext:
    """Translate ``SSLConfig`` into httpx's supported ``verify=`` forms.

    httpx 0.28 deprecates ``verify=<str>`` and ``cert=``: anything beyond a
    plain boolean must arrive as a ready context. A non-bool ``verify`` is a
    scenario-relative CA bundle file or directory; a client cert needs a context
    to load into, so a bool ``verify`` is expanded via
    ``httpx.create_ssl_context``, keeping httpx's own trust-store semantics.
    """
    verify = config.verify
    if not isinstance(verify, bool):
        ca = resolve_scenario_path(scenario_dir, verify)
        ctx = ssl.create_default_context(capath=ca) if ca.is_dir() else ssl.create_default_context(cafile=ca)
    elif config.cert is None:
        return verify
    else:
        ctx = httpx.create_ssl_context(verify=verify)

    if config.cert is not None:
        if isinstance(config.cert, list | tuple):
            certfile, keyfile = (resolve_scenario_path(scenario_dir, p) for p in config.cert)
            ctx.load_cert_chain(certfile, keyfile)
        else:
            ctx.load_cert_chain(resolve_scenario_path(scenario_dir, config.cert))
    return ctx


# A scenario without a `client` block: the defaults, for builders called without one.
_DEFAULT_CLIENT = ClientConfig()

# The `client` settings that are not text, with what they must be.
_CLIENT_SETTING_KINDS = {
    "timeout": "a number",
    "follow_redirects": "a boolean",
    "max_redirects": "a whole number",
    "http2": "a boolean",
    "max_connections": "a whole number",
    "max_keepalive_connections": "a whole number",
}


def _client_setting(client: ClientConfig, name: str) -> Any:
    """A resolved ``client`` setting that is not text, or a stage failure.

    Its template branch accepts any complete template, so one that rendered to
    another template arrives here as text, which httpx would take as it is: a
    truthy ``http2``, a timeout failing only on the first request.
    """
    value = getattr(client, name)
    if isinstance(value, str):
        raise StageExecutionError(f"client.{name} must resolve to {_CLIENT_SETTING_KINDS[name]}, got {value!r}")
    return value


def _client_url(client: ClientConfig, name: str) -> str | None:
    """A resolved ``client.base_url`` or ``client.proxy``, or a stage failure.

    As in `_client_setting`, a template that rendered to template text passes
    the field's template branch (the URL branch refuses a template), and httpx
    would then fail it without saying where it came from: a proxy with
    "Unknown scheme", a base URL on every request as missing its protocol. The
    value is not quoted, as the URL fields' own messages do not quote it: it
    can carry credentials (`validate_proxy_url`).
    """
    value = getattr(client, name)
    if value is not None and re.search(TEMPLATE_PATTERN, value):
        raise StageExecutionError(f"client.{name} must resolve to a URL, but it rendered to text with a template expression in it (not shown, as it can carry credentials)")
    return value


def _proxy(url: str, ssl_config: SSLConfig, scenario_dir: Path | None) -> str | httpx.Proxy:
    """``client.proxy`` as httpx takes it: an ``https`` proxy's own TLS
    connection checked with the scenario's ``ssl`` settings, once it has any.

    Given a URL, httpx leaves the proxy's TLS to httpcore's default context
    whatever ``verify`` is: a proxy signed by a private CA failed even with
    ``verify: false``, and ``ssl.cert`` never reached it. That default (the
    system's CAs and certifi's bundle) is what httpx checks a proxy from
    ``HTTPS_PROXY`` against, so a scenario with the default ``ssl`` keeps it:
    what ``verify: true`` means for the servers, certifi's bundle alone, would
    refuse a proxy signed by a CA only the system trusts. Otherwise the proxy
    gets a context of its own, built from the same settings as the servers':
    httpcore sets the ALPN protocols on the context of each connection it
    opens, HTTP/1.1 only for a proxy, and with one context for both, another
    thread opening a server connection could offer the proxy HTTP/2.
    """
    # The model has parsed it with httpx already (`validate_proxy_url`).
    if httpx.URL(url).scheme != "https" or (ssl_config.verify is True and ssl_config.cert is None):
        return url
    verify = build_ssl_verify(ssl_config, scenario_dir)
    context = verify if isinstance(verify, ssl.SSLContext) else httpx.create_ssl_context(verify=verify)
    return httpx.Proxy(url, ssl_context=context)


class _BearerAuth(httpx.Auth):
    """``Authorization: Bearer <token>`` on every request, set as httpx's own
    BasicAuth sets its header: over one the request's headers carry."""

    def __init__(self, token: str) -> None:
        self._auth_header = f"Bearer {token}"

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = self._auth_header
        yield request


class _DigestAuth(httpx.DigestAuth):
    """httpx's DigestAuth, sending each attempt as a request of its own, and
    safe to share between threads.

    httpx answers a challenge by adding the Authorization header to the
    request it sent first and sending that again, so the 401 in the response's
    history, which the HAR file shows, pointed at a request carrying the
    credentials it had gone out without. The flow is httpx's; what it yields
    goes out as a copy taken then (`_copy`).

    The scenario's digest auth is the shared client's, which a parallel stage's
    threads send through at once. Each step of httpx's flow reads and writes
    the server's last challenge and the count of its nonce's uses, which it
    increments without a lock: two requests could go out with the same ``nc``,
    which a server enforcing RFC 7616's replay protection refuses. So each
    step runs under a lock of the instance's; the exchanges between them do not.

    httpx also restarts the count at 1 for every challenge it answers, the
    nonce already in use included: requests challenged together (a parallel
    stage that is the scenario's first, a server handing one nonce to all of
    them) all answered with ``nc=00000001``, and so did a request challenged
    again after the count had moved on. The count restarts for a new nonce
    only (`_answer_on`).

    That builds on httpx.DigestAuth's private state, which no release promises
    to keep: ``_last_challenge`` (its ``nonce``), ``_nonce_count`` and
    ``_build_auth_header(request, challenge)``, as of httpx 0.28. The digest
    tests in tests/unit/test_request_builder.py fail if a new httpx changes
    them (``test_digest_builds_on_httpx_internals`` first, naming them), and
    pyproject.toml's httpx requirement points here.
    """

    def __init__(self, username: str, password: str) -> None:
        super().__init__(username, password)
        self._lock = threading.Lock()

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        flow = super().auth_flow(request)
        with self._lock:
            to_send = next(flow)
        while True:
            response = yield self._copy(to_send)
            with self._lock:
                nonce, count = self._nonce(), self._nonce_count
                try:
                    to_send = flow.send(response)
                except StopIteration:
                    return
                self._answer_on(to_send, nonce, count)

    def _nonce(self) -> bytes | None:
        return self._last_challenge.nonce if self._last_challenge is not None else None

    def _answer_on(self, request: httpx.Request, nonce: bytes | None, count: int) -> None:
        """httpx just answered a challenge in ``request``, restarting ``nc`` at
        1; ``nonce`` and ``count`` are the ones in use before it. For the same
        nonce, the answer is built again on its count."""
        challenge = self._last_challenge
        if challenge is not None and nonce is not None and challenge.nonce == nonce:
            self._nonce_count = count
            request.headers["Authorization"] = self._build_auth_header(request, challenge)

    @staticmethod
    def _copy(request: httpx.Request) -> httpx.Request:
        """``request`` as it is now, sharing its body stream, as httpx's
        redirect follow-ups do. A request built on a stream is not read, so a
        user function's ``response.request.content`` would raise
        ``RequestNotRead``: a ``ByteStream`` is read back, as httpx reads one
        it builds from ``content=`` (plain bytes, replayable). Any other stream
        is left unread, as it is in the request the stage built."""
        copy = httpx.Request(request.method, request.url, headers=request.headers.copy(), stream=request.stream, extensions=dict(request.extensions))
        if isinstance(copy.stream, httpx.ByteStream):
            copy.read()
        return copy


def _names_by_template(declared: RequestAuth | None) -> bool:
    """Whether ``declared``, an auth as written, names a user function with a
    template, whole (``"auth": "{{ creds }}"``) or in part."""
    match declared:
        case UserFunctionName(root=name) | UserFunctionKwargs(name=UserFunctionName(root=name)):
            return contains_template(name)
        case _:
            return False


# How a failure refers to a user function whose name a template rendered.
_RENDERED_NAME = "auth's template rendered a user function name (not shown, as auth can carry a credential)"


def _call_named_by_template(auth: UserFunctionName | UserFunctionKwargs) -> Any:
    """`call_user_function`, for a name a template rendered, which a failure
    does not quote.

    The name was never in the file, and a template renders what it was written
    for: over basic credentials, ``"auth": "{{ creds }}"`` renders
    ``"user:password"``, a valid name, so it is imported as one, and the
    failure named the user name as the module and the password as the
    function. A failure to import says what fails (`import_function`'s
    ``quoted``) and where a credential goes.
    """
    name, kwargs = call_target(auth)
    try:
        func = import_function(name, quoted=False)
    except UserFunctionError as e:
        raise UserFunctionError(f"""{_RENDERED_NAME} that does not import: {e}; a built-in scheme is an object, such as {{"bearer": "<token>"}}""") from None
    try:
        return func(**kwargs)
    except Exception as e:
        raise UserFunctionError(f"{_RENDERED_NAME}, and calling it failed: {e}") from e


def build_auth(auth: Auth, declared: RequestAuth | None = None) -> Any:
    """What httpx takes as ``auth=`` for a resolved `Auth`: a built-in scheme's
    flow, or whatever the user function returns (any form httpx accepts).

    A new flow per call: a request's digest auth answers its own challenge, and
    only the scenario's, built once for the shared client, carries the server's
    nonce from one request to the next. A failing user function raises
    `UserFunctionError`. ``declared`` is the auth as written, which ``auth``
    rendered; a user function's name a template in it rendered is not quoted
    (`_call_named_by_template`).
    """
    match auth:
        case BasicAuth(basic=credentials):
            return httpx.BasicAuth(credentials.username, credentials.password)
        case DigestAuth(digest=credentials):
            return _DigestAuth(credentials.username, credentials.password)
        case BearerAuth(bearer=token):
            return _BearerAuth(token)
        case UserFunctionName() | UserFunctionKwargs() if _names_by_template(declared):
            return _call_named_by_template(auth)
        case UserFunctionName() | UserFunctionKwargs():
            return call_user_function(auth)
        case _:
            raise RuntimeError(f"Unhandled auth: {type(auth).__name__}")


def build_client_kwargs(client: ClientConfig, ssl_config: SSLConfig, auth: Auth | None, scenario_dir: Path | None, *, declared_auth: Auth | None = None) -> dict[str, Any]:
    """Arguments for the scenario's shared client, from its resolved
    ``client``, ``ssl`` and ``auth``. ``auth`` is built here (`build_auth`)
    because httpx wants the resulting flow, not its description;
    ``declared_auth`` is the scenario's as written.

    ``client.params`` is not among them: httpx would merge it into every URL's
    query by decoding and re-encoding the whole query, which is what
    `build_request_kwargs` merges params without. The pool has no connection
    limit unless ``max_connections`` sets one: httpx's default of 100 silently
    capped a parallel stage's ``max_concurrency``, which bounds it already.
    That is HTTP/1.1's cap. Over HTTP/2 httpcore sends all the requests to an
    origin down one connection and holds it to 100 streams, which no pool
    setting lifts; ``http2: false`` does (docs/advanced/parallel.md). An
    ``https`` proxy's TLS takes ``ssl`` too (`_proxy`).
    """
    setting = partial(_client_setting, client)
    kwargs: dict[str, Any] = {
        "verify": build_ssl_verify(ssl_config, scenario_dir),
        "headers": client.headers,
        "timeout": setting("timeout"),
        "follow_redirects": setting("follow_redirects"),
        "max_redirects": setting("max_redirects"),
        "http2": setting("http2"),
        "limits": httpx.Limits(max_connections=setting("max_connections"), max_keepalive_connections=setting("max_keepalive_connections")),
    }
    if (base_url := _client_url(client, "base_url")) is not None:
        kwargs["base_url"] = base_url
    if (proxy := _client_url(client, "proxy")) is not None:
        kwargs["proxy"] = _proxy(proxy, ssl_config, scenario_dir)
    if auth is not None:
        kwargs["auth"] = build_auth(auth, declared_auth)
    return kwargs


def _read_file(path: Path, declared: Any, missing: str, unreadable: str) -> bytes:
    """Read a scenario-referenced file, reporting I/O failures as `RequestError`
    against the path as written in the scenario."""
    try:
        return path.read_bytes()
    except FileNotFoundError as e:
        raise RequestError(f"{missing}: {declared}") from e
    except OSError as e:
        raise RequestError(f"{unreadable} '{declared}': {e}") from e


def _encode_param(key: str, value: Any) -> str:
    """``key``'s ``key=value`` segment(s), encoded as httpx encodes params.

    httpx turns each value into text with ``str()``, which a whole-string
    template's raw value can refuse: an int past Python's digit limit, or an
    object whose ``__str__`` raises. That runs before the request is sent, so
    it is caught here and reported against the parameter as a `RequestError`,
    instead of escaping as the raw exception.
    """
    try:
        return str(httpx.QueryParams({key: value}))
    except Exception as e:
        raise RequestError(f"Cannot convert query parameter '{key}' to text: {type(e).__name__}: {e}") from e


def _merge_query(query: str, params: dict[str, Any], defaults: Mapping[str, Any]) -> str:
    """The URL's raw ``query`` with ``params`` merged in, and ``defaults``
    (``client.params``) for the keys neither sets.

    Key order and precedence are httpx's ``URL.copy_merge_params``: the URL's
    parameters first, then new keys in declared order, and a key in both takes
    the params value(s) at its first place in the URL, its other occurrences
    dropped. The merge works on the raw ``&``-separated segments, though: a
    segment whose key params does not name goes out as written, and only keys
    are decoded, to match. copy_merge_params decodes the whole query into a
    dict and encodes it again, which changed parameters params never names:
    ``q=%E9`` (not UTF-8) went out as ``q=%EF%BF%BD``, ``a=1&b=2&a=3`` as
    ``a=1&a=3&b=2``, and ``a=1;b=2`` as ``a=1%3Bb%3D2``.

    A default fills in a key only: whatever the stage says about it, in its
    URL or its params, wins. It goes ahead of params' new keys, where httpx
    puts a client's params.
    """
    url_keys = {unquote_plus(segment.partition("=")[0]) for segment in query.split("&") if segment}
    params = {**{key: value for key, value in defaults.items() if key not in url_keys}, **params}
    pending = {key: _encode_param(key, value) for key, value in params.items()}
    segments = []
    for segment in query.split("&"):
        key = unquote_plus(segment.partition("=")[0])
        if key not in params:
            segments.append(segment)
        elif key in pending:
            segments.append(pending.pop(key))
    segments.extend(pending.values())
    # Empty segments (from `&&`, a bare `?`, or an empty list value) carry no parameter.
    return "&".join(segment for segment in segments if segment)


def _declares_content_type(headers: Iterable[str]) -> bool:
    return any(name.lower() == "content-type" for name in headers)


def build_request_kwargs(
    request_model: Request,
    scenario_dir: Path | None = None,
    client: ClientConfig | None = None,
    redaction: Redaction = DEFAULT_REDACTION,
    *,
    declared_auth: RequestAuth | None = None,
) -> dict[str, Any]:
    """Arguments for one ``client.request(...)`` call from a resolved `Request`,
    sent on the shared client that the resolved ``client`` configured.

    The client applies its own ``base_url``, ``headers``, ``timeout`` and
    ``follow_redirects``, so the request carries a timeout or a redirect
    setting only where the stage declared one (``model_fields_set``, which a
    value from ``$include``/``$merge`` is in): the model's defaults would
    override the client's. ``client.params`` is merged here (`_merge_query`).
    A message quoting the URL shows it through ``redaction``, as the report
    does. ``declared_auth`` is the stage's ``auth`` as written (`build_auth`).
    """
    client = client if client is not None else _DEFAULT_CLIENT
    url = request_model.url
    if client.base_url is None and is_relative_url(url):
        # Sent anyway, httpx would fail it as missing its protocol, which does
        # not say where the URL was meant to come from.
        raise RequestError(f"Request URL {redaction.url(url)!r} is relative, but the scenario sets no client.base_url to resolve it against")

    if request_model.params or client.params:
        # httpx's params= *replaces* the URL's own query, so `/items?page=2`
        # with params {"limit": 10} went out as `/items?limit=10`. Merged into
        # the URL instead, which moves parsing — and its InvalidURL — here
        # from client.request. httpx.URL has already normalized the query to
        # what it would send without params, so the kept segments match that.
        try:
            parsed = httpx.URL(url)
            query = _merge_query(parsed.query.decode("ascii"), request_model.params, client.params)
            url = str(parsed.copy_with(query=query.encode("ascii") if query else None))
        except httpx.InvalidURL as e:
            raise RequestError(f"Invalid request URL: {e}") from e

    request_kwargs: dict[str, Any] = {
        "method": request_model.method,
        "url": url,
        "headers": request_model.headers,
    }
    declared = request_model.model_fields_set
    if "timeout" in declared:
        request_kwargs["timeout"] = request_model.timeout
    if "allow_redirects" in declared:
        request_kwargs["follow_redirects"] = request_model.allow_redirects

    match request_model.auth:
        case None:
            # Left out: httpx applies the client's, the scenario's auth.
            pass
        case False:
            # httpx takes None as "no auth for this request", where leaving
            # the argument out (its USE_CLIENT_DEFAULT) takes the client's. A
            # URL's userinfo still applies, as it does on a client without one.
            request_kwargs["auth"] = None
        case auth:
            try:
                request_kwargs["auth"] = build_auth(auth, declared_auth)
            except UserFunctionError as e:
                raise RequestError(f"Failed to configure authentication: {e}") from e

    # The Content-Type a form or multipart body is encoded for (see below).
    body_content_type: str | None = None

    match request_model.body:
        case None:
            pass

        case JsonBody(json=None):
            # httpx reads json=None as "no body", so a declared null — literal,
            # or a template that rendered to None — went out exactly like an
            # undeclared body. Sent as the JSON document it is instead.
            request_kwargs["content"] = b"null"
            if not _declares_content_type((*client.headers, *request_model.headers)):
                request_kwargs["headers"] = {**request_model.headers, "Content-Type": "application/json"}

        case JsonBody(json=data):
            request_kwargs["json"] = data

        case GraphQLBody(graphql=gql):
            request_kwargs["json"] = {"query": gql.query, "variables": gql.variables}

        case FormBody(form=data):
            request_kwargs["data"] = data
            if data:
                body_content_type = "application/x-www-form-urlencoded"

        case XmlBody(xml=data) | TextBody(text=data):
            request_kwargs["content"] = data

        case Base64Body(base64=encoded_data):
            request_kwargs["content"] = base64.b64decode(encoded_data)

        case BinaryBody(binary=file_path):
            path = resolve_scenario_path(scenario_dir, file_path)
            request_kwargs["content"] = _read_file(path, file_path, "Binary file not found", "Cannot read binary file")

        case FilesBody(files=file_paths):
            files_list = []
            for field_name, file_path in file_paths.items():
                path = resolve_scenario_path(scenario_dir, file_path)
                content = _read_file(path, file_path, "File not found for upload", "Cannot read file for upload")
                files_list.append((field_name, (path.name, content)))
            request_kwargs["files"] = files_list
            if files_list:
                # httpx encodes the parts with the boundary a multipart
                # Content-Type header names, so this one is the one used.
                body_content_type = f"multipart/form-data; boundary={os.urandom(16).hex()}"

        case _:
            raise RuntimeError(f"Unhandled request body type: {type(request_model.body).__name__}")

    if body_content_type is not None and _declares_content_type(client.headers) and not _declares_content_type(request_model.headers):
        # httpx gives a body's own Content-Type only to a request that has
        # none, and a client header counts: a scenario-wide
        # `Content-Type: application/json` labelled every form body JSON and
        # dropped the multipart boundary, which a stage cannot supply. A body
        # encoded one way keeps its type; a stage's own header still wins.
        request_kwargs["headers"] = {**request_model.headers, "Content-Type": body_content_type}

    return request_kwargs
