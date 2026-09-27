"""request_builder: resolved Request models -> httpx kwargs.

The success path of every body type runs end to end in
tests/integration/test_body_types.py; this pins the mapping details and the
error paths a server round trip cannot reach.
"""

import httpx
import pytest

from pytest_httpchain.errors import RequestError
from pytest_httpchain.models import BinaryBody, FilesBody, Request
from pytest_httpchain.request_builder import build_request_kwargs


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


def _sent(request: Request) -> httpx.Request:
    """The request httpx puts on the wire for ``request``'s kwargs."""
    sent = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200))) as client:
        client.request(**build_request_kwargs(request))
    return sent[0]


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


@pytest.mark.parametrize(("declared", "follow"), [({}, True), ({"allow_redirects": False}, False)], ids=["default", "disabled"])
def test_allow_redirects_maps_to_follow_redirects(declared, follow):
    """httpx defaults follow_redirects to False, so silently dropping the
    mapping would flip the plugin's documented follow-by-default behavior."""
    assert build_request_kwargs(Request.model_validate({"url": "http://t/", **declared}))["follow_redirects"] is follow


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
