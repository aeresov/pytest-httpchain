"""request_builder: resolved Request models -> httpx kwargs.

The success path of every body type runs end to end in
tests/integration/test_body_types.py; this pins the mapping details and the
error paths a server round trip cannot reach.
"""

import inspect
import json
import re
import ssl
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote_to_bytes

import httpx
import msgpack
import pytest
import trustme

from pytest_httpchain.errors import RequestError, StageExecutionError
from pytest_httpchain.models import (
    AuthCredentials,
    BasicAuth,
    BearerAuth,
    BinaryBody,
    ClientConfig,
    DigestAuth,
    FilesBody,
    MultipartBody,
    Request,
    SSLConfig,
    UserFunctionKwargs,
    UserFunctionName,
)
from pytest_httpchain.models.types import _PROXY_SCHEMES
from pytest_httpchain.redaction import Redaction
from pytest_httpchain.request_builder import build_auth, build_client_kwargs, build_request_kwargs
from pytest_httpchain.userfunc import UserFunctionError
from pytest_httpchain.validation import load_scenario


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param(lambda d: BinaryBody(binary="/nonexistent/file.bin"), "Binary file not found", id="binary-missing"),
        pytest.param(lambda d: FilesBody(files={"upload": "/nonexistent/file.txt"}), "File not found for upload", id="files-missing"),
        # A directory raises IsADirectoryError: an OSError that is NOT a
        # FileNotFoundError, so only a broadened handler turns it into a
        # stage failure (M2).
        pytest.param(lambda d: BinaryBody(binary=str(d)), "Cannot read binary file", id="binary-unreadable"),
        pytest.param(lambda d: FilesBody(files={"upload": str(d)}), "Cannot read file for upload", id="files-unreadable"),
        # A NUL (a JSON "\u0000", or a template's) is refused by the path
        # call itself, with a ValueError that escaped as a plugin error.
        pytest.param(lambda d: BinaryBody(binary="a\x00b"), "Cannot read binary file 'a\x00b': ", id="binary-nul"),
        pytest.param(lambda d: FilesBody(files={"upload": "a\x00b"}), "Cannot read file for upload 'a\x00b': ", id="files-nul"),
        # Named as the scenario gives it, not where it resolved to: a file
        # object's path, and a list's item. The model holds it as a Path, so
        # tidied as `binary`'s is (and as `validate --deep` names it).
        pytest.param(lambda d: MultipartBody(multipart={"files": {"doc": {"path": "missing.pdf"}}}), "File not found for upload: missing.pdf", id="multipart-object-missing"),
        pytest.param(
            lambda d: MultipartBody(multipart={"files": {"doc": "./sub//missing.pdf"}}),
            f"File not found for upload: {Path('sub', 'missing.pdf')}",
            id="multipart-path-tidied",
        ),
        pytest.param(lambda d: FilesBody(files={"docs": [str(d), "gone/x.txt"]}), "Cannot read file for upload", id="files-list-unreadable"),
    ],
)
def test_unreadable_body_file_is_a_request_error(tmp_path, body, message):
    with pytest.raises(RequestError, match=re.escape(message)):
        build_request_kwargs(Request(url="https://example.com/api", method="POST", body=body(tmp_path)))


def _exchange(request: Request, client: ClientConfig | None = None, respond=lambda request: httpx.Response(200)) -> tuple[list[httpx.Request], httpx.Response]:
    """What httpx puts on the wire for ``request``'s kwargs, on the shared
    client ``client`` configures (both builders, as the carrier uses them),
    and the response it ends with."""
    client = client if client is not None else ClientConfig()
    sent = []
    transport = httpx.MockTransport(lambda r: sent.append(r) or respond(r))
    with httpx.Client(**build_client_kwargs(client, SSLConfig(), None, None), transport=transport) as http:
        response = http.request(**build_request_kwargs(request, client=client))
    return sent, response


def _sent(request: Request, client: ClientConfig | None = None) -> httpx.Request:
    """The one request httpx puts on the wire for ``request``."""
    [sent], _ = _exchange(request, client)
    return sent


def test_msgpack_body_and_binary_query_parameter():
    packet = {"b": b"\x00\xff", "name": "device"}
    sent = _sent(Request(url="http://t/", method="POST", body={"msgpack": packet}, params={"packet": msgpack.packb(packet, use_bin_type=True)}))
    assert sent.headers["content-type"] == "application/msgpack"
    assert msgpack.unpackb(sent.content, raw=False) == packet
    assert b"packet=%82" in sent.url.query
    assert b"%00%FF" in sent.url.query


def test_form_preserves_packed_binary_field_and_ordinary_values():
    packet = {"id": 42, "b": b"\x00\xff\x80&+=%"}
    packed = msgpack.packb(packet, use_bin_type=True)
    form = {"packet": packed, "note": "a b", "tag": ["one", "two"], "flag": True, "empty": None}
    sent = _sent(Request(url="http://t/", method="POST", body={"form": form}), ClientConfig(headers={"Content-Type": "application/json"}))

    def fields():
        for part in sent.content.split(b"&"):
            key, _, value = part.partition(b"=")
            yield unquote_to_bytes(key.replace(b"+", b" ")), unquote_to_bytes(value.replace(b"+", b" "))

    assert sent.headers["content-type"] == "application/x-www-form-urlencoded"
    assert list(fields()) == [
        (b"packet", packed),
        (b"note", b"a b"),
        (b"tag", b"one"),
        (b"tag", b"two"),
        (b"flag", b"true"),
        (b"empty", b""),
    ]
    assert msgpack.unpackb(dict(fields())[b"packet"], raw=False) == packet


def test_msgpack_body_overrides_client_content_type_but_not_stage_content_type():
    request = Request(url="http://t/", method="POST", body={"msgpack": None})
    assert _sent(request, ClientConfig(headers={"Content-Type": "application/json"})).headers["content-type"] == "application/msgpack"
    custom = Request(url="http://t/", method="POST", body={"msgpack": None}, headers={"Content-Type": "application/vnd.example+msgpack"})
    assert _sent(custom, ClientConfig(headers={"Content-Type": "application/json"})).headers["content-type"] == "application/vnd.example+msgpack"


def test_bytes_body_must_resolve_to_bytes():
    with pytest.raises(RequestError, match="The bytes body must resolve to bytes"):
        build_request_kwargs(Request(url="http://t/", method="POST", body={"bytes": "{{ packet }}"}))


@pytest.mark.parametrize(
    ("url", "params", "sent_url"),
    [
        # httpx's params= replaced the URL's query outright, dropping page=2.
        pytest.param("http://t/items?page=2", {"limit": 10}, "http://t/items?page=2&limit=10", id="url-query-kept"),
        # A shared key keeps the URL's position and takes the params value.
        pytest.param("http://t/items?page=2&sort=name", {"page": 3}, "http://t/items?page=3&sort=name", id="params-win-shared-key"),
        # ...at its first place, and its other occurrences go.
        pytest.param("http://t/items?a=1&b=2&a=3", {"a": 9}, "http://t/items?a=9&b=2", id="shared-key-every-occurrence"),
        pytest.param("http://t/items?so%72t=name", {"sort": "price"}, "http://t/items?sort=price", id="shared-key-matched-decoded"),
        pytest.param("http://t/items?tag=a", {"tag": ["b", "c"]}, "http://t/items?tag=b&tag=c", id="list-value-repeats-key"),
        pytest.param("http://t/items?tag=a", {"tag": []}, "http://t/items", id="empty-list-drops-key"),
        # The rest keep their raw bytes. Merging via httpx's copy_merge_params
        # decoded the query into a dict and re-encoded it: %E9 became U+FFFD
        # (%EF%BF%BD), repeats were regrouped (a=1&a=3&b=2), `flag` gained an
        # `=`, %20 became +, and `,` `;` `=` inside a value were escaped.
        pytest.param("http://t/items?q=%E9", {"x": 1}, "http://t/items?q=%E9&x=1", id="non-utf8-escape-kept"),
        pytest.param("http://t/items?a=1&b=2&a=3", {"x": 1}, "http://t/items?a=1&b=2&a=3&x=1", id="url-repeats-keep-order"),
        pytest.param("http://t/items?flag&q=a%20b&f=a,b;c=d", {"x": 1}, "http://t/items?flag&q=a%20b&f=a,b;c=d&x=1", id="url-segments-verbatim"),
        # httpx reads an explicit params={} as "replace the query with nothing".
        pytest.param("http://t/items?page=2", {}, "http://t/items?page=2", id="no-params"),
    ],
)
def test_params_merge_into_url_query(url, params, sent_url):
    assert str(_sent(Request(url=url, params=params)).url) == sent_url


@pytest.mark.parametrize(
    ("url", "raw_path"),
    [
        # Sent WHATWG-normalized, this probe reached /ok instead.
        pytest.param("http://t/static/%2e%2e/ok", b"/static/%2e%2e/ok", id="encoded-dot-segment"),
        pytest.param("http://t/a\\b", b"/a\\b", id="backslash"),
        pytest.param("http://t/" + "a" * 3000, b"/" + b"a" * 3000, id="over-2083-chars"),
    ],
)
@pytest.mark.parametrize("params", [{}, {"x": 1}], ids=["no-params", "params-merged"])
def test_url_is_sent_as_written(url, raw_path, params):
    query = b"?x=1" if params else b""
    assert _sent(Request(url=url, params=params)).url.raw_path == raw_path + query


def test_unparseable_url_is_a_request_error():
    """Merging parses the URL before client.request does, so httpx's
    InvalidURL must still arrive as a stage failure. A URL still carrying a
    template marker is the one kind model validation lets through unparsed."""
    with pytest.raises(RequestError, match="Invalid request URL: Invalid port"):
        build_request_kwargs(Request(url="http://t:port/{{ x }}", params={"limit": 10}))


class _Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("no text form")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        # A whole-string template keeps its raw value, and params are Any, so
        # the str() happens in httpx's param encoding — which ran outside any
        # handler and escaped as the raw ValueError with the plugin's traceback.
        pytest.param(2**100000, r"ValueError: Exceeds the limit \(4300 digits\)", id="int-past-digit-limit"),
        pytest.param(_Unprintable(), "RuntimeError: no text form", id="str-raises"),
        pytest.param(["ok", _Unprintable()], "RuntimeError: no text form", id="list-item-str-raises"),
    ],
)
def test_param_without_text_form_is_a_request_error(value, message):
    with pytest.raises(RequestError, match=f"Cannot convert query parameter 'n' to text: {message}"):
        build_request_kwargs(Request(url="http://t/items?page=2", params={"n": value}))


def _redirect_to_end(request: httpx.Request) -> httpx.Response:
    return httpx.Response(302, headers={"Location": "/end"}) if request.url.path == "/start" else httpx.Response(200)


@pytest.mark.parametrize(
    ("client", "declared", "follow"),
    [
        # httpx defaults follow_redirects to False: dropping the default would
        # flip the plugin's documented follow-by-default behavior.
        pytest.param({}, {}, True, id="default"),
        pytest.param({}, {"allow_redirects": False}, False, id="stage-disables"),
        pytest.param({"follow_redirects": False}, {}, False, id="client-disables"),
        # The stage's declared value wins, even where it equals the model default.
        pytest.param({"follow_redirects": False}, {"allow_redirects": True}, True, id="stage-wins"),
    ],
)
def test_follow_redirects_precedence(client, declared, follow):
    sent, response = _exchange(Request.model_validate({"url": "http://t/start", **declared}), ClientConfig.model_validate(client), _redirect_to_end)
    assert (len(sent), response.status_code) == ((2, 200) if follow else (1, 302))


@pytest.mark.parametrize(
    ("client", "declared", "timeout"),
    [
        pytest.param({}, {}, 30.0, id="default"),
        pytest.param({"timeout": 5}, {}, 5, id="client"),
        pytest.param({}, {"timeout": 60}, 60, id="stage"),
        pytest.param({"timeout": 5}, {"timeout": 60}, 60, id="stage-wins"),
        # Declared, not defaulted: a stage writing the default still wins.
        pytest.param({"timeout": 5}, {"timeout": 30.0}, 30.0, id="stage-declares-the-default"),
    ],
)
def test_timeout_precedence(client, declared, timeout):
    sent = _sent(Request.model_validate({"url": "http://t/", **declared}), ClientConfig.model_validate(client))
    assert sent.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), timeout)


@pytest.mark.parametrize("directive", ["$include", "$merge"])
def test_timeout_and_redirects_from_a_reference_count_as_declared(tmp_path, directive):
    """A value a stage takes from a fragment is its own, as documented: the
    loader resolves references into the raw scenario before the model sees
    it, so the value is in model_fields_set. Were fragments applied after
    validation, the fragment's 30 would lose to client.timeout."""
    (tmp_path / "request.json").write_text(json.dumps({"timeout": 30, "allow_redirects": True}))
    scenario_file = tmp_path / "test_ref.http.json"
    scenario_file.write_text(
        json.dumps(
            {
                "client": {"base_url": "http://t", "timeout": 5, "follow_redirects": False},
                "stages": [{"name": "fragment", "request": {directive: "request.json", "url": "/x"}}],
            }
        )
    )
    scenario, _ = load_scenario(scenario_file, root_path=tmp_path)

    kwargs = build_request_kwargs(scenario.stages[0].request, tmp_path, scenario.client)
    assert (kwargs["timeout"], kwargs["follow_redirects"]) == (30, True)


def _count_down(request: httpx.Request) -> httpx.Response:
    """/2 redirects to /1, /1 to /0, which answers."""
    remaining = int(request.url.path[1:])
    return httpx.Response(302, headers={"Location": f"/{remaining - 1}"}) if remaining else httpx.Response(200)


def test_max_redirects_bounds_a_redirect_chain():
    assert _exchange(Request(url="http://t/2"), ClientConfig(max_redirects=2), _count_down)[1].status_code == 200
    with pytest.raises(httpx.TooManyRedirects):
        _exchange(Request(url="http://t/2"), ClientConfig(max_redirects=1), _count_down)


def test_client_headers_are_sent_and_stage_headers_override_by_name():
    """Case-insensitively, as httpx merges them: the stage's spelling is the one sent."""
    sent = _sent(Request(url="http://t/", headers={"accept": "text/plain"}), ClientConfig(headers={"X-Api-Key": "k", "Accept": "application/json"}))
    assert (sent.headers.get_list("x-api-key"), sent.headers.get_list("accept")) == (["k"], ["text/plain"])


@pytest.mark.parametrize(
    ("url", "params", "defaults", "sent_url"),
    [
        pytest.param("http://t/items", {}, {"api_key": "k"}, "http://t/items?api_key=k", id="added"),
        # httpx's own order: the client's keys first, then the stage's new ones.
        pytest.param("http://t/items", {"page": 2}, {"api_key": "k"}, "http://t/items?api_key=k&page=2", id="ahead-of-stage-params"),
        pytest.param("http://t/items", {"v": 2}, {"v": 1, "api_key": "k"}, "http://t/items?v=2&api_key=k", id="stage-params-win"),
        # A default never overrides what the stage wrote, in its URL either:
        # httpx's client params would have replaced api_key=mine.
        pytest.param("http://t/items?api_key=mine", {}, {"api_key": "k"}, "http://t/items?api_key=mine", id="url-query-wins"),
        pytest.param("http://t/items?so%72t=name", {}, {"sort": "price"}, "http://t/items?so%72t=name", id="url-key-matched-decoded"),
        # An empty list removes the key, the default with it.
        pytest.param("http://t/items", {"tag": []}, {"tag": "x"}, "http://t/items", id="stage-removes-key"),
        # The URL's own query keeps its bytes: httpx's client params would
        # have decoded and re-encoded it, and dropped q=%E9.
        pytest.param("http://t/items?q=%E9&flag", {}, {"a": 1}, "http://t/items?q=%E9&flag&a=1", id="url-query-as-written"),
    ],
)
def test_client_params_fill_in_keys_the_stage_does_not_set(url, params, defaults, sent_url):
    assert str(_sent(Request(url=url, params=params), ClientConfig(params=defaults)).url) == sent_url


@pytest.mark.parametrize(
    ("base_url", "url", "raw_path"),
    [
        # httpx appends the relative URL's path to the base path, a leading
        # '/' included: it does not reset the path to the host's root.
        pytest.param("http://t/v1", "/users/1", b"/v1/users/1", id="leading-slash"),
        pytest.param("http://t/v1", "users/1", b"/v1/users/1", id="no-leading-slash"),
        pytest.param("http://t/v1/", "/users/1", b"/v1/users/1", id="base-trailing-slash"),
        pytest.param("http://t", "/users/1", b"/users/1", id="base-without-path"),
        pytest.param("http://t/v1", "?page=2", b"/v1/?page=2", id="query-only"),
        pytest.param("http://t/v1", "/static/%2e%2e/ok", b"/v1/static/%2e%2e/ok", id="as-written"),
        # Literal dot segments are resolved, as in an absolute URL.
        pytest.param("http://t/v1", "../x", b"/x", id="dot-segment"),
    ],
)
def test_relative_url_joins_base_url(base_url, url, raw_path):
    sent = _sent(Request(url=url), ClientConfig(base_url=base_url))
    assert (sent.url.host, sent.url.raw_path) == ("t", raw_path)


def test_absolute_url_ignores_base_url():
    assert str(_sent(Request(url="http://other/x"), ClientConfig(base_url="http://t/v1")).url) == "http://other/x"


def test_relative_url_params_merge_before_the_join():
    request = Request(url="/items?page=2", params={"limit": 10})
    assert str(_sent(request, ClientConfig(base_url="http://t/v1", params={"api_key": "k"})).url) == "http://t/v1/items?page=2&api_key=k&limit=10"


@pytest.mark.parametrize("client", [None, ClientConfig(proxy="http://proxy.test:3128")], ids=["no-client", "client-without-base-url"])
def test_relative_url_without_base_url_is_a_request_error(client):
    """A template can render to a relative URL the validator could not see
    (HTTPCHAIN034 covers the literal ones). Sent, httpx would fail it for a
    missing protocol, which does not say where the URL should come from."""
    with pytest.raises(RequestError, match=r"^Request URL '/users/1' is relative, but the scenario sets no client.base_url to resolve it against$"):
        build_request_kwargs(Request(url="/users/1"), client=client)


def test_relative_url_error_shows_the_url_redacted():
    """The message prints above the report, whose URL hides the same value."""
    with pytest.raises(RequestError, match=re.escape("Request URL '/ok?api_key=[REDACTED]&page=2' is relative")):
        build_request_kwargs(Request(url="/ok?api_key=s3cret&page=2"))
    with pytest.raises(RequestError, match=re.escape("Request URL '/ok?sig=[REDACTED]' is relative")):
        build_request_kwargs(Request(url="/ok?sig=s3cret"), redaction=Redaction(query_params=["sig"]))


def _digest_server(request: httpx.Request) -> httpx.Response:
    """A digest challenge for a request without credentials, 200 for one with:
    computing the answer is httpx's, and checking it flask-httpauth's in the
    integration suite."""
    if request.headers.get("authorization", "").startswith("Digest "):
        return httpx.Response(200)
    return httpx.Response(401, headers={"WWW-Authenticate": 'Digest realm="r", nonce="n1", qop="auth", opaque="o"'})


class _Transport(httpx.BaseTransport):
    """``handler``'s response to each request, which it gets as a real
    transport does: unread. httpx's MockTransport reads every request before
    its handler sees it, which would hide a request whose ``.content`` a user
    function cannot read."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return self._handler(request)


class TestAuth:
    BASIC = BasicAuth(basic=AuthCredentials(username="u", password="p"))

    @staticmethod
    def _wire(requests: list[Request], scenario_auth=None, respond=lambda request: httpx.Response(200)) -> tuple[list[httpx.Request], list[httpx.Response]]:
        """What goes on the wire for ``requests``, sent in turn on one client
        built with the scenario's ``scenario_auth``, and their responses."""
        sent = []
        transport = _Transport(lambda request: sent.append(request) or respond(request))
        with httpx.Client(**build_client_kwargs(ClientConfig(), SSLConfig(), scenario_auth, None), transport=transport) as http:
            responses = [http.request(**build_request_kwargs(request)) for request in requests]
        return sent, responses

    @pytest.mark.parametrize(
        ("auth", "authorization"),
        [
            pytest.param(BASIC, "Basic dTpw", id="basic"),
            pytest.param(BearerAuth(bearer="tok"), "Bearer tok", id="bearer"),
        ],
    )
    @pytest.mark.parametrize("level", ["scenario", "request"])
    def test_builtin_sets_the_authorization_header(self, auth, authorization, level):
        request = Request(url="http://t/", auth=auth if level == "request" else None)
        [sent], _ = self._wire([request], scenario_auth=auth if level == "scenario" else None)
        assert sent.headers.get_list("authorization") == [authorization]

    def test_builtin_replaces_an_authorization_header_the_stage_sets(self):
        """As httpx's BasicAuth does: the scheme sets the header, whatever the
        request's headers say."""
        [sent], _ = self._wire([Request(url="http://t/", headers={"Authorization": "Token old"}, auth=BearerAuth(bearer="tok"))])
        assert sent.headers.get_list("authorization") == ["Bearer tok"]

    def test_digest_replaces_the_stages_header_when_it_answers(self):
        """Digest has nothing to set before the server's challenge: the first
        request goes out with the header the stage wrote, and the answer
        replaces it."""
        request = Request(url="http://t/", headers={"Authorization": "Token old"}, auth=DigestAuth(digest=AuthCredentials(username="u", password="p")))
        (challenged, answer), [response] = self._wire([request], respond=_digest_server)
        assert response.status_code == 200
        assert challenged.headers.get_list("authorization") == ["Token old"]
        assert [value.split(" ", 1)[0] for value in answer.headers.get_list("authorization")] == ["Digest"]

    @pytest.mark.parametrize(
        ("method", "body", "content"),
        [
            pytest.param("POST", {"json": {"a": 1}}, b'{"a":1}', id="post"),
            pytest.param("GET", None, b"", id="get"),
        ],
    )
    def test_digest_answers_the_challenge_in_a_request_of_its_own(self, method, body, content):
        """httpx's flow added the answer to the request it had sent first, so
        the challenge in the history, which the HAR file shows, carried
        credentials it went out without. Both are requests a user function can
        read: built on the first one's body stream, the copies were unread, so
        ``response.request.content`` raised ``RequestNotRead``."""
        request = Request(url="http://t/", method=method, auth=DigestAuth(digest=AuthCredentials(username="u", password="p")), body=body)
        (challenged, answer), [response] = self._wire([request], respond=_digest_server)
        assert response.status_code == 200
        [challenge] = response.history
        assert (challenge.request, response.request) == (challenged, answer)
        assert "authorization" not in challenged.headers
        assert answer.headers["authorization"].startswith('Digest username="u", realm="r", nonce="n1", uri="/"')
        # The body goes out with both.
        assert challenged.content == answer.content == content

    def test_digest_counts_each_nonce_use_once_across_threads(self, monkeypatch):
        """The scenario's digest auth is the shared client's, which a parallel
        stage's threads send through at once. httpx reads the nonce count and
        increments it without a lock, around computing the client nonce (made
        slow here, to widen that window): the threads sent the same count,
        which a server enforcing replay protection refuses."""
        client_nonce = httpx.DigestAuth._get_client_nonce

        def slow_client_nonce(self, nonce_count, nonce):
            time.sleep(0.01)
            return client_nonce(self, nonce_count, nonce)

        monkeypatch.setattr(httpx.DigestAuth, "_get_client_nonce", slow_client_nonce)
        sent = []
        transport = _Transport(lambda request: sent.append(request) or _digest_server(request))
        with httpx.Client(**build_client_kwargs(ClientConfig(), SSLConfig(), DigestAuth(digest=AuthCredentials(username="u", password="p")), None), transport=transport) as http:
            http.get("http://t/")  # Challenged: from here on, answered up front.
            with ThreadPoolExecutor(max_workers=8) as pool:
                statuses = list(pool.map(lambda _: http.get("http://t/").status_code, range(8)))
        assert statuses == [200] * 8
        assert sorted(self._counts(sent)) == [f"{n:08x}" for n in range(1, 10)]

    def test_digest_builds_on_httpx_internals(self):
        """What `_DigestAuth` counts nonce uses with is httpx.DigestAuth's
        private state (as of httpx 0.28): if a new httpx renames it, every
        digest request fails with an AttributeError, and this names what
        changed."""
        auth = httpx.DigestAuth("u", "p")
        assert (auth._last_challenge, auth._nonce_count) == (None, 1)
        assert list(inspect.signature(auth._build_auth_header).parameters) == ["request", "challenge"]

    @staticmethod
    def _counts(sent: list[httpx.Request]) -> list[str]:
        """The ``nc`` of each answer that went out, in the order sent."""
        return [re.search(r"nc=(\w+)", request.headers["authorization"]).group(1) for request in sent if "authorization" in request.headers]

    def test_digest_counts_on_when_challenged_together_with_one_nonce(self):
        """httpx restarts the count at 1 for each challenge it answers, the
        nonce in use included: a parallel stage that is the scenario's first,
        its requests challenged at once with the one nonce a server hands out
        (RFC 7616 allows it), answered all with nc=00000001, which a server
        checking for replays refuses but for one. The count restarts for a new
        nonce only."""
        arrived = threading.Barrier(8, timeout=10)
        sent = []

        def respond(request):
            if "authorization" not in request.headers:
                arrived.wait()  # All eight are challenged before any answers.
            return _digest_server(request)

        transport = _Transport(lambda request: sent.append(request) or respond(request))
        with httpx.Client(**build_client_kwargs(ClientConfig(), SSLConfig(), DigestAuth(digest=AuthCredentials(username="u", password="p")), None), transport=transport) as http:
            with ThreadPoolExecutor(max_workers=8) as pool:
                statuses = list(pool.map(lambda _: http.get("http://t/").status_code, range(8)))
        assert statuses == [200] * 8
        assert sorted(self._counts(sent)) == [f"{n:08x}" for n in range(1, 9)]

    @pytest.mark.parametrize(
        ("nonce", "counts"),
        [
            # Challenged again with the nonce it answered with (a server's
            # replay check refusing an answer, say): the count goes on.
            pytest.param("n1", ["00000001", "00000002", "00000003"], id="same-nonce-counts-on"),
            # A new nonce (a stale one replaced): its count starts at 1.
            pytest.param("n2", ["00000001", "00000002", "00000001"], id="new-nonce-restarts"),
        ],
    )
    def test_digest_rechallenged(self, nonce, counts):
        answers = iter([200, 401, 200])

        def respond(request):
            if "authorization" not in request.headers:
                return _digest_server(request)
            if (status := next(answers)) == 401:
                return httpx.Response(401, headers={"WWW-Authenticate": f'Digest realm="r", nonce="{nonce}", qop="auth", opaque="o"'})
            return httpx.Response(status)

        scenario_auth = DigestAuth(digest=AuthCredentials(username="u", password="p"))
        sent, responses = self._wire([Request(url="http://t/"), Request(url="http://t/")], scenario_auth=scenario_auth, respond=respond)
        assert [response.status_code for response in responses] == [200, 200]
        assert self._counts(sent) == counts
        assert f'nonce="{nonce}"' in sent[-1].headers["authorization"]

    @pytest.mark.parametrize(
        ("level", "wire_requests"),
        [
            # The scenario's flow is the shared client's: it answers the second
            # request up front with the nonce the first was challenged with.
            pytest.param("scenario", 3, id="scenario-reuses-the-nonce"),
            # A request's is built for it, and is challenged each time.
            pytest.param("request", 4, id="request-challenged-each-time"),
        ],
    )
    def test_digest_challenge_is_answered_once_per_flow(self, level, wire_requests):
        auth = DigestAuth(digest=AuthCredentials(username="u", password="p"))
        request = Request(url="http://t/", auth=auth if level == "request" else None)
        sent, responses = self._wire([request, request], scenario_auth=auth if level == "scenario" else None, respond=_digest_server)
        assert [response.status_code for response in responses] == [200, 200]
        assert len(sent) == wire_requests

    @pytest.mark.parametrize(
        ("auth", "authorization"),
        [
            pytest.param(None, ["Basic dTpw"], id="left-out-takes-the-scenarios"),
            # httpx's auth=None, where leaving it out is USE_CLIENT_DEFAULT.
            pytest.param(False, [], id="false-sends-none"),
            pytest.param(BearerAuth(bearer="tok"), ["Bearer tok"], id="request-auth-replaces-the-scenarios"),
        ],
    )
    def test_request_auth_over_the_scenarios(self, auth, authorization):
        [sent], _ = self._wire([Request(url="http://t/", auth=auth)], scenario_auth=self.BASIC)
        assert sent.headers.get_list("authorization") == authorization

    def test_false_leaves_a_urls_userinfo_to_httpx(self):
        """As on a client without auth: httpx sends the userinfo as Basic
        credentials, which only the scenario's auth would have replaced."""
        [sent], _ = self._wire([Request(url="http://a:b@t/", auth=False)], scenario_auth=BearerAuth(bearer="tok"))
        assert sent.headers.get_list("authorization") == ["Basic YTpi"]

    def test_failing_user_function_is_a_request_error(self):
        """Named in the file, the name is quoted, as any user function's is."""
        with pytest.raises(RequestError, match="Failed to configure authentication: Failed to import module 'nosuchmodule_xyz'"):
            build_request_kwargs(Request(url="http://t/", auth="nosuchmodule_xyz:auth"), declared_auth=UserFunctionName("nosuchmodule_xyz:auth"))

    RENDERED_NAME = "auth's template rendered a user function name (not shown, as auth can carry a credential)"

    @pytest.mark.parametrize(
        ("declared", "rendered", "reason"),
        [
            # Basic credentials written as the whole auth: "user:password" has
            # a name's shape, so it is imported as one, and the failure named
            # the user name as the module, and the password as the function
            # when the user name is a module's (test, os, secrets...).
            pytest.param("{{ creds }}", "s3cret_user:s3cret_pass", "importing its module raised ModuleNotFoundError", id="whole-no-module"),
            pytest.param("{{ creds }}", "json:s3cret_pass", "its module has no function of that name", id="whole-no-function"),
            # A name a template renders in part was not in the file either.
            pytest.param({"name": "json:{{ fn }}"}, {"name": "json:s3cret_pass"}, "its module has no function of that name", id="in-part"),
        ],
    )
    @pytest.mark.parametrize("level", ["scenario", "request"])
    def test_function_name_a_template_rendered_is_not_quoted(self, declared, rendered, reason, level):
        declared = Request.model_validate({"url": "http://t/", "auth": declared}).auth
        auth = Request.model_validate({"url": "http://t/", "auth": rendered}).auth
        if level == "scenario":
            with pytest.raises(UserFunctionError) as excinfo:
                build_client_kwargs(ClientConfig(), SSLConfig(), auth, None, declared_auth=declared)
        else:
            with pytest.raises(RequestError) as excinfo:
                build_request_kwargs(Request(url="http://t/", auth=auth), declared_auth=declared)
        assert f"{self.RENDERED_NAME} that does not import: {reason}; a built-in scheme is an object" in str(excinfo.value)
        assert "s3cret" not in str(excinfo.value)

    def test_function_a_template_named_is_called(self):
        """A name a template rendered that is a function's is imported and
        called as any other; a failure calling it does not name it either."""
        declared = UserFunctionKwargs.model_validate({"name": "{{ fn }}", "kwargs": {"username": "u", "password": "p"}})
        auth = UserFunctionKwargs.model_validate({"name": "httpx:BasicAuth", "kwargs": {"username": "u", "password": "p"}})
        client_kwargs = build_client_kwargs(ClientConfig(), SSLConfig(), auth, None, declared_auth=declared)
        with httpx.Client(**client_kwargs, transport=_Transport(lambda request: httpx.Response(200))) as http:
            assert http.get("http://t/").request.headers["authorization"] == "Basic dTpw"
        with pytest.raises(UserFunctionError, match=re.escape(f"{self.RENDERED_NAME}, and calling it failed: ")):
            build_auth(UserFunctionName("httpx:BasicAuth"), UserFunctionName("{{ fn }}"))


class TestClientKwargs:
    def test_defaults(self):
        """The pool has no connection limit: httpx's default of 100 silently
        capped a parallel stage's max_concurrency (150 concurrent requests of
        a second took two). The rest are today's defaults."""
        kwargs = build_client_kwargs(ClientConfig(), SSLConfig(), None, None)
        assert kwargs == {
            "verify": True,
            "headers": {},
            "timeout": 30.0,
            "follow_redirects": True,
            "max_redirects": 20,
            "http2": True,
            "limits": httpx.Limits(max_connections=None, max_keepalive_connections=20),
        }

    def test_every_setting_reaches_the_client(self):
        client = ClientConfig(
            base_url="https://api.test/v1",
            headers={"X": "1"},
            timeout=5,
            follow_redirects=False,
            max_redirects=3,
            proxy="http://proxy.test:3128",
            http2=False,
            max_connections=8,
            max_keepalive_connections=None,
        )
        kwargs = build_client_kwargs(client, SSLConfig(), None, None)
        assert {name: kwargs[name] for name in kwargs if name != "verify"} == {
            "base_url": "https://api.test/v1",
            "headers": {"X": "1"},
            "timeout": 5,
            "follow_redirects": False,
            "max_redirects": 3,
            "proxy": "http://proxy.test:3128",
            "http2": False,
            "limits": httpx.Limits(max_connections=8, max_keepalive_connections=None),
        }
        # And httpx takes them: the proxy is mounted for every URL.
        with httpx.Client(**kwargs) as http:
            assert (str(http.base_url), http.max_redirects) == ("https://api.test/v1/", 3)

    @pytest.mark.parametrize("verify", [True, False], ids=["default-ssl", "verify-off"])
    @pytest.mark.parametrize("scheme", _PROXY_SCHEMES)
    def test_httpx_takes_every_proxy_scheme_the_model_accepts(self, scheme, verify):
        """httpx before 0.28 refused socks5h only once the client was built,
        failing a scenario that `validate` had passed: the httpx floor is
        this test's to keep honest (the lowest-floor CI job runs it). With
        ``ssl`` set, only an https proxy gets a TLS context: httpcore refuses
        one for an http proxy."""
        client = ClientConfig(proxy=f"{scheme}://proxy.test:1080")
        httpx.Client(**build_client_kwargs(client, SSLConfig(verify=verify), None, None)).close()

    @pytest.mark.parametrize(
        ("proxy", "verify"),
        [
            # httpx then checks the proxy's certificate as it checks one from
            # HTTPS_PROXY, against the system's CAs and certifi's: wider than
            # what `verify: true` means for the servers, certifi's alone.
            ("https://proxy.test:3128", True),
            ("http://proxy.test:3128", False),
            ("socks5://proxy.test:1080", False),
        ],
    )
    def test_proxy_left_to_httpx(self, proxy, verify):
        assert build_client_kwargs(ClientConfig(proxy=proxy), SSLConfig(verify=verify), None, None)["proxy"] == proxy

    def test_https_proxy_takes_the_ssl_settings_in_a_context_of_its_own(self, tmp_path):
        """Given the URL alone, httpx checked the proxy against certifi's
        bundle whatever ``ssl`` said. The context is not the servers':
        httpcore sets each connection's ALPN protocols on its context, and
        only HTTP/1.1 on a proxy's."""
        ca = trustme.CA()
        ca.cert_pem.write_to_path(tmp_path / "ca.pem")
        ca.issue_cert("client@example.com").private_key_and_cert_chain_pem.write_to_path(tmp_path / "client.pem")
        client = ClientConfig(proxy="https://user:pw@proxy.test:3128")
        kwargs = build_client_kwargs(client, SSLConfig(verify="ca.pem", cert="client.pem"), None, tmp_path)

        proxy = kwargs["proxy"]
        assert isinstance(proxy, httpx.Proxy)
        assert (str(proxy.url), proxy.auth) == ("https://proxy.test:3128", ("user", "pw"))
        assert isinstance(proxy.ssl_context, ssl.SSLContext)
        assert proxy.ssl_context is not kwargs["verify"]
        assert proxy.ssl_context.cert_store_stats()["x509_ca"] == 1
        httpx.Client(**kwargs).close()

    def test_verify_off_reaches_an_https_proxy(self):
        proxy = build_client_kwargs(ClientConfig(proxy="https://proxy.test:3128"), SSLConfig(verify=False), None, None)["proxy"]
        assert (proxy.ssl_context.verify_mode, proxy.ssl_context.check_hostname) == (ssl.CERT_NONE, False)

    def test_params_are_left_to_the_request_builder(self):
        """httpx would merge them into every URL's query by decoding and
        re-encoding it, which the request builder avoids."""
        assert "params" not in build_client_kwargs(ClientConfig(params={"a": 1}), SSLConfig(), None, None)

    @pytest.mark.parametrize(
        ("field", "kind"),
        [
            ("timeout", "a number"),
            ("follow_redirects", "a boolean"),
            ("max_redirects", "a whole number"),
            ("http2", "a boolean"),
            ("max_connections", "a whole number"),
            ("max_keepalive_connections", "a whole number"),
        ],
    )
    def test_residual_template_is_refused(self, field, kind):
        """The template branch accepts any complete template, so one that
        rendered to another template arrives as text, which httpx would take
        as it is: a truthy http2, a timeout that fails only when used."""
        client = ClientConfig.model_validate({field: "{{ x }}"})
        with pytest.raises(StageExecutionError, match=re.escape(f"client.{field} must resolve to {kind}, got '{{{{ x }}}}'")):
            build_client_kwargs(client, SSLConfig(), None, None)

    @pytest.mark.parametrize("field", ["base_url", "proxy"])
    def test_residual_template_in_a_url_is_refused_unquoted(self, field):
        """As above: httpx would fail it later without naming the setting, a
        proxy as an unknown scheme, a base URL on every request as missing its
        protocol. Not quoted, as the URL can carry credentials."""
        client = ClientConfig.model_validate({field: "http://user:s3cret@{{ host }}"})
        message = f"client.{field} must resolve to a URL, but it rendered to text with a template expression in it"
        with pytest.raises(StageExecutionError, match=f"^{re.escape(message)}") as e:
            build_client_kwargs(client, SSLConfig(), None, None)
        assert "s3cret" not in str(e.value)


@pytest.mark.parametrize(
    ("headers", "content_type"),
    [({}, "application/json"), ({"content-type": "application/vnd.api+json"}, "application/vnd.api+json")],
    ids=["default-content-type", "declared-content-type"],
)
def test_json_null_is_sent_as_a_json_document(headers, content_type):
    """httpx reads ``json=None`` as "no body": a declared null (literal, or a
    template that rendered to None) went out as an empty request with no
    content type, exactly like an undeclared body."""
    sent = _sent(Request.model_validate({"url": "http://t/", "method": "POST", "headers": headers, "body": {"json": None}}))
    assert sent.content == b"null"
    assert sent.headers.get_list("content-type") == [content_type]


def test_json_null_keeps_the_clients_content_type():
    """As httpx's own json= does: a content type the client sends is not
    overridden by the JSON default."""
    request = Request.model_validate({"url": "http://t/", "method": "POST", "body": {"json": None}})
    sent = _sent(request, ClientConfig(headers={"Content-Type": "application/vnd.api+json"}))
    assert sent.headers.get_list("content-type") == ["application/vnd.api+json"]


@pytest.mark.parametrize(
    ("body", "headers", "content_type"),
    [
        # httpx gives a body's own type only to a request without one, and a
        # client header counts: this form went out labelled JSON, the server
        # parsed no fields, and a stage could not have supplied the multipart
        # boundary. A body encoded one way keeps its type.
        pytest.param({"form": {"a": "1"}}, {}, "application/x-www-form-urlencoded", id="form-keeps-its-type"),
        pytest.param({"files": {"upload": "upload.txt"}}, {}, "multipart/form-data; boundary=", id="files-keep-their-type"),
        pytest.param({"multipart": {"fields": {"a": "1"}}}, {}, "multipart/form-data; boundary=", id="multipart-keeps-its-type"),
        pytest.param({"form": {"a": "1"}}, {"content-type": "text/plain"}, "text/plain", id="stage-wins-over-form"),
        # Any other body takes the client's, as httpx's own json= does: a JSON
        # API's media type is what a scenario-wide Content-Type is for.
        pytest.param({"json": {"a": 1}}, {}, "application/vnd.api+json", id="json-takes-the-clients"),
        pytest.param({"text": "{}"}, {}, "application/vnd.api+json", id="text-takes-the-clients"),
        # An empty form is no body at all, so nothing is encoded to keep.
        pytest.param({"form": {}}, {}, "application/vnd.api+json", id="empty-form"),
    ],
)
def test_client_content_type_and_encoded_bodies(tmp_path, monkeypatch, body, headers, content_type):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "upload.txt").write_text("hello")
    request = Request.model_validate({"url": "http://t/", "method": "POST", "headers": headers, "body": body})
    sent = _sent(request, ClientConfig(headers={"Content-Type": "application/vnd.api+json"}))
    [sent_type] = sent.headers.get_list("content-type")
    assert sent_type.startswith(content_type)
    if content_type.startswith("multipart/"):
        # The parts are delimited by the boundary the header names.
        boundary = sent_type.partition("boundary=")[2]
        assert sent.read().startswith(f"--{boundary}\r\n".encode())


def _part(name: str, content: bytes, filename: str | None = None, content_type: str | None = None, boundary: str = "XyZ") -> bytes:
    """One part of a multipart body as httpx encodes it."""
    disposition = f'Content-Disposition: form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
    headers = disposition + (f"\r\nContent-Type: {content_type}" if content_type else "")
    return f"--{boundary}\r\n{headers}\r\n\r\n".encode() + content + b"\r\n"


class TestMultipart:
    """``body.multipart``, and ``body.files`` with it: the parts are encoded
    here, by httpx, and sent as bytes, which the report and the HAR export can
    then show, with the Content-Type naming their boundary."""

    # A stage's own Content-Type names the boundary: the body is spelled out.
    HEADERS = {"Content-Type": "multipart/form-data; boundary=XyZ"}

    def _kwargs(self, tmp_path, body, headers=HEADERS) -> dict:
        return build_request_kwargs(Request.model_validate({"url": "http://t/", "method": "POST", "headers": headers, "body": body}), tmp_path)

    def test_fields_then_files_with_their_defaults(self, tmp_path):
        """Fields first, each list item a part of the same name, as httpx
        writes a form value (true, not True). A file's filename defaults to
        its path's last component, or to its field's name; its content type
        is guessed from the filename, else application/octet-stream. An
        empty filename is sent as none."""
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "report.pdf").write_bytes(b"%PDF")
        (tmp_path / "b.png").write_bytes(b"PNG")
        body = {
            "multipart": {
                "fields": {"title": "Report", "tags": ["a", 2], "draft": False, "final": True, "ratio": 1.5},
                "files": {
                    "document": "docs/report.pdf",
                    "images": [{"path": "b.png", "filename": "photo.jpg", "content_type": "image/webp"}, {"content": "x", "filename": "c.txt"}],
                    "note": {"content": "héllo"},
                    "blob": {"base64": "AAE="},
                    "meta": {"content": "{}", "filename": "", "content_type": "application/json"},
                },
            }
        }
        kwargs = self._kwargs(tmp_path, body)
        assert kwargs["headers"] == self.HEADERS
        assert kwargs["content"] == b"".join(
            [
                _part("title", b"Report"),
                _part("tags", b"a"),
                _part("tags", b"2"),
                _part("draft", b"false"),
                _part("final", b"true"),
                _part("ratio", b"1.5"),
                _part("document", b"%PDF", "report.pdf", "application/pdf"),
                _part("images", b"PNG", "photo.jpg", "image/webp"),
                _part("images", b"x", "c.txt", "text/plain"),
                _part("note", "héllo".encode(), "note", "application/octet-stream"),
                _part("blob", b"\x00\x01", "blob", "application/octet-stream"),
                _part("meta", b"{}", None, "application/json"),
                b"--XyZ--\r\n",
            ]
        )

    def test_files_body_sends_its_file_objects_and_lists_alike(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"A")
        kwargs = self._kwargs(tmp_path, {"files": {"doc": "a.txt", "more": ["a.txt", {"content": "B", "filename": "b.png"}]}})
        assert kwargs["content"] == b"".join(
            [_part("doc", b"A", "a.txt", "text/plain"), _part("more", b"A", "a.txt", "text/plain"), _part("more", b"B", "b.png", "image/png"), b"--XyZ--\r\n"]
        )

    def test_without_parts_the_body_is_its_closing_delimiter(self, tmp_path):
        """A list rendered empty leaves no part: still a multipart body, as a
        form without inputs sends one, not none (httpx would send nothing)."""
        assert self._kwargs(tmp_path, {"multipart": {"fields": {"tags": []}}})["content"] == b"--XyZ--\r\n"

    def test_boundary_is_fresh_and_named_in_the_content_type(self, tmp_path):
        kwargs = self._kwargs(tmp_path, {"multipart": {"fields": {"a": "1"}}}, headers={"X-Other": "1"})
        content_type = kwargs["headers"]["Content-Type"]
        boundary = re.fullmatch("multipart/form-data; boundary=([0-9a-f]{32})", content_type).group(1)
        assert kwargs["headers"]["X-Other"] == "1"
        assert kwargs["content"] == _part("a", b"1", boundary=boundary) + f"--{boundary}--\r\n".encode()
        assert self._kwargs(tmp_path, {"multipart": {"fields": {"a": "1"}}}, headers={})["headers"]["Content-Type"] != content_type

    def test_stage_content_type_is_sent_and_its_boundary_used(self, tmp_path):
        """Whatever multipart type the stage declares: httpx took the
        boundary of multipart/form-data alone, and delimited the parts of
        any other with one of its own, which the header did not name."""
        headers = {"content-type": 'multipart/mixed; boundary="a b"'}
        kwargs = self._kwargs(tmp_path, {"multipart": {"fields": {"a": "1"}}}, headers=headers)
        assert kwargs["headers"] == headers
        assert kwargs["content"] == _part("a", b"1", boundary="a b") + b"--a b--\r\n"

    @pytest.mark.parametrize(
        ("content_type", "sent"),
        [
            # Written out of habit, and sent as written (httpx's way) it named
            # no boundary for the server to find the parts by.
            ("multipart/form-data", "multipart/form-data; boundary={}"),
            ("Multipart/Mixed; charset=utf-8;", "Multipart/Mixed; charset=utf-8; boundary={}"),
        ],
    )
    def test_multipart_content_type_without_a_boundary_gets_the_bodys(self, tmp_path, content_type, sent):
        kwargs = self._kwargs(tmp_path, {"multipart": {"fields": {"a": "1"}}}, headers={"Content-Type": content_type, "X-Other": "1"})
        boundary = re.fullmatch(rb"--([0-9a-f]{32})\r\n.*", kwargs["content"], re.DOTALL).group(1).decode()
        assert kwargs["headers"] == {"Content-Type": sent.format(boundary), "X-Other": "1"}
        assert kwargs["content"] == _part("a", b"1", boundary=boundary) + f"--{boundary}--\r\n".encode()

    @pytest.mark.parametrize("content_type", ["text/plain", "text/plain; boundary=", "multipart"])
    def test_other_content_type_without_a_boundary_is_sent_as_written(self, tmp_path, content_type):
        """Not a multipart type (``multipart`` alone is none), it is the
        stage's to send as it is: the parts are delimited by a fresh boundary."""
        kwargs = self._kwargs(tmp_path, {"multipart": {"fields": {"a": "1"}}}, headers={"Content-Type": content_type})
        assert kwargs["headers"] == {"Content-Type": content_type}
        assert re.fullmatch(rb'--([0-9a-f]{32})\r\nContent-Disposition: form-data; name="a"\r\n\r\n1\r\n--\1--\r\n', kwargs["content"])

    @pytest.mark.parametrize("content_type", ["multipart/form-data; boundary=", 'multipart/mixed; boundary=""'])
    def test_multipart_content_type_with_an_empty_boundary_is_a_request_error(self, tmp_path, content_type):
        with pytest.raises(RequestError, match=re.escape(f"The stage's Content-Type {content_type!r} names an empty boundary")):
            self._kwargs(tmp_path, {"files": {"f": {"content": "x"}}}, headers={"Content-Type": content_type})

    @pytest.mark.parametrize(
        ("content_type", "boundary"),
        [
            # httpx reads the boundary back from a Content-Type cut at ';',
            # so these parts went out delimited by `a` (email unquotes it).
            ('multipart/form-data; boundary="a;b"', "a;b"),
            ("multipart/form-data; boundary*=utf-8''a%3Bb", "a;b"),
            # Whitespace at its end httpx strips, and get_boundary() too.
            ('multipart/form-data; boundary="abc "', "abc "),
            ('multipart/mixed; boundary="abc\t"', "abc\t"),
            # A quote at either end httpx strips.
            ('multipart/form-data; boundary="\\"ab"', '"ab'),
            ('multipart/form-data; boundary="ab\\""', 'ab"'),
            # Any type naming one: its boundary delimits the parts.
            ('text/plain; boundary="x;y"', "x;y"),
        ],
    )
    @pytest.mark.parametrize("fields", [{"a": "1"}, {"tags": []}], ids=["parts", "no-parts"])
    def test_boundary_httpx_cannot_carry_as_declared_is_a_request_error(self, tmp_path, content_type, boundary, fields):
        """Not the one the parts would be delimited by, it left the server no
        fields and no error. Refused whether or not there are parts, which
        the plugin writes itself when there are none: a header either works
        or does not, whatever a template renders."""
        with pytest.raises(RequestError, match=re.escape(f"The stage's Content-Type {content_type!r} names the boundary {boundary!r}, which cannot delimit the parts as declared")):
            self._kwargs(tmp_path, {"multipart": {"fields": fields}}, headers={"Content-Type": content_type})

    @pytest.mark.parametrize(("quoted", "boundary"), [('" ab"', " ab"), ('"a\\"b"', 'a"b'), ('"a b"', "a b"), ('"a\tb"', "a\tb")])
    def test_boundary_httpx_carries_is_the_one_used(self, tmp_path, quoted, boundary):
        """Whitespace at the start, a quote or whitespace inside: httpx reads
        them back as they are."""
        headers = {"Content-Type": f"multipart/form-data; boundary={quoted}"}
        kwargs = self._kwargs(tmp_path, {"multipart": {"fields": {"a": "1"}}}, headers=headers)
        assert kwargs["content"] == _part("a", b"1", boundary=boundary) + f"--{boundary}--\r\n".encode()

    def test_sent_as_encoded(self, tmp_path):
        """On the wire: the bytes, with the Content-Type the client would
        otherwise replace."""
        request = Request.model_validate({"url": "http://t/", "method": "POST", "body": {"multipart": {"fields": {"a": "1"}, "files": {"f": {"content": "x"}}}}})
        sent = _sent(request, ClientConfig(headers={"Content-Type": "application/json"}))
        boundary = sent.headers["content-type"].partition("boundary=")[2]
        assert sent.read() == _part("a", b"1", boundary=boundary) + _part("f", b"x", "f", "application/octet-stream", boundary=boundary) + f"--{boundary}--\r\n".encode()
        assert sent.headers["content-length"] == str(len(sent.read()))

    @pytest.mark.parametrize(
        ("body", "headers", "message"),
        [
            # A template rendered to template text passes the field's template
            # branch; binascii's error escaped as a plugin error, and text
            # holding a character outside ASCII raises a plain ValueError.
            pytest.param({"multipart": {"files": {"f": {"base64": "{{ x }}"}}}}, {}, "The base64 of file 'f' is not valid base64", id="file-base64-template-text"),
            pytest.param({"base64": "{{ x }}"}, {}, "The base64 body is not valid base64", id="base64-body-template-text"),
            pytest.param(
                {"multipart": {"files": {"f": {"base64": "é{{ x }}"}}}},
                {},
                "The base64 of file 'f' is not valid base64: it holds a character outside ASCII",
                id="file-base64-template-text-not-ascii",
            ),
            pytest.param({"base64": "é{{ x }}"}, {}, "The base64 body is not valid base64: it holds a character outside ASCII", id="base64-body-template-text-not-ascii"),
            # Past Python's digit limit an int has no text form.
            pytest.param({"multipart": {"fields": {"n": 10**5000}}}, {}, "Cannot convert multipart field 'n' to text", id="int-past-digit-limit"),
            # A lone surrogate, which a template can render, has no UTF-8 form.
            pytest.param({"multipart": {"fields": {"n": "\udc80"}}}, {}, "Cannot encode multipart field 'n' as UTF-8", id="field-surrogate"),
            pytest.param({"multipart": {"files": {"f": {"content": "\udc80"}}}}, {}, "Cannot encode the content of file 'f' as UTF-8", id="content-surrogate"),
            pytest.param({"multipart": {"fields": {"\udc80": "v"}}}, {}, "Cannot encode the multipart body", id="name-surrogate"),
            pytest.param({"multipart": {"fields": {"n": "v"}}}, {"Content-Type": "multipart/form-data; boundary=é"}, "Cannot encode the multipart body", id="boundary-not-ascii"),
        ],
    )
    def test_unencodable_body_is_a_request_error(self, tmp_path, body, headers, message):
        with pytest.raises(RequestError, match=re.escape(message)):
            self._kwargs(tmp_path, body, headers=headers)
