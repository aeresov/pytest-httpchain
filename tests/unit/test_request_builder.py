"""request_builder: resolved Request models -> httpx kwargs.

The success path of every body type runs end to end in
tests/integration/test_body_types.py; this pins the mapping details and the
error paths a server round trip cannot reach.
"""

import json
import re
import ssl

import httpx
import pytest
import trustme

from pytest_httpchain.errors import RequestError, StageExecutionError
from pytest_httpchain.models import BinaryBody, ClientConfig, FilesBody, Request, SSLConfig
from pytest_httpchain.models.types import _PROXY_SCHEMES
from pytest_httpchain.redaction import Redaction
from pytest_httpchain.request_builder import build_client_kwargs, build_request_kwargs
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
    ],
)
def test_unreadable_body_file_is_a_request_error(tmp_path, body, message):
    with pytest.raises(RequestError, match=message):
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
