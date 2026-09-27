import json
import tracemalloc

import httpx
import pytest

from pytest_httpchain.report_formatter import format_request, format_response
from tests.unit.helpers import TOO_DEEP_TO_PARSE, on_bounded_stack

_UNDECODABLE = bytes(range(256))
_BIG_JSON = {"data": ["x" * 50] * 200}
_JSON = {"content-type": "application/json"}
# Parses on every supported interpreter, but pretty-prints in full to 12.5 MB.
_DEEP_JSON = b"[" * 2_500 + b"]" * 2_500


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        pytest.param(
            httpx.Request("GET", "https://example.com/api/users", params={"page": "1", "limit": "10"}, headers={"authorization": "Bearer token123", "x-custom": "value"}),
            "GET https://example.com/api/users?page=1&limit=10\nhost: example.com\nauthorization: Bearer token123\nx-custom: value\n",
            id="no-body",
        ),
        pytest.param(
            # Bytes, not json=: httpx's own JSON encoding (and so content-length)
            # differs across the supported httpx range.
            httpx.Request("POST", "https://example.com/api/users", headers={"content-type": "application/json"}, content=json.dumps({"name": "Alice", "age": 30}).encode()),
            'POST https://example.com/api/users\nhost: example.com\ncontent-type: application/json\ncontent-length: 28\n\n{\n  "name": "Alice",\n  "age": 30\n}',
            id="json-pretty-printed",
        ),
        pytest.param(
            httpx.Request("POST", "https://example.com/api/data", headers={"content-type": "text/plain"}, content=b"Hello, World!"),
            "POST https://example.com/api/data\nhost: example.com\ncontent-type: text/plain\ncontent-length: 13\n\nHello, World!",
            id="text",
        ),
        pytest.param(
            httpx.Request("POST", "https://example.com/api/login", data={"username": "alice", "password": "secret"}),
            "POST https://example.com/api/login\nhost: example.com\ncontent-length: 30\ncontent-type: application/x-www-form-urlencoded\n\nusername=alice&password=secret",
            id="form",
        ),
        # Declared JSON that fails to parse still decodes as text, so it is
        # shown as text — not mislabeled binary.
        pytest.param(
            httpx.Request("POST", "https://example.com/api/data", headers={"content-type": "application/json"}, content=b"{not valid json"),
            "POST https://example.com/api/data\nhost: example.com\ncontent-type: application/json\ncontent-length: 15\n\n{not valid json",
            id="malformed-json-as-text",
        ),
        # Only genuinely undecodable bytes earn the binary label.
        pytest.param(
            httpx.Request("POST", "https://example.com/api/upload", headers={"content-type": "application/octet-stream"}, content=_UNDECODABLE),
            "POST https://example.com/api/upload\nhost: example.com\ncontent-type: application/octet-stream\ncontent-length: 256\n\n<Binary content: 256 bytes>",
            id="undecodable-as-binary",
        ),
        # A streaming body is consumed on send and never buffered.
        pytest.param(
            httpx.Request("POST", "https://example.com/upload", content=iter([b"chunk"])),
            "POST https://example.com/upload\nhost: example.com\ntransfer-encoding: chunked\n\n<Streaming body (e.g. multipart file upload): consumed on send, not captured>",
            id="streaming-placeholder",
        ),
    ],
)
def test_format_request(request_, expected):
    assert format_request(request_) == expected


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        pytest.param(
            httpx.Response(200, headers={"content-type": "text/plain", "x-request-id": "abc123", "cache-control": "no-cache"}, content=b"OK"),
            "HTTP/1.1 200 OK\ncontent-type: text/plain\nx-request-id: abc123\ncache-control: no-cache\ncontent-length: 2\n\nOK",
            id="text",
        ),
        pytest.param(
            httpx.Response(500, headers={"content-type": "text/html"}, content=b"<html><body>Internal Server Error</body></html>"),
            "HTTP/1.1 500 Internal Server Error\ncontent-type: text/html\ncontent-length: 47\n\n<html><body>Internal Server Error</body></html>",
            id="html",
        ),
        pytest.param(
            httpx.Response(200, headers={"content-type": "application/json"}, content=json.dumps({"id": 1, "name": "Alice"}).encode()),
            'HTTP/1.1 200 OK\ncontent-type: application/json\ncontent-length: 26\n\n{\n  "id": 1,\n  "name": "Alice"\n}',
            id="json-pretty-printed",
        ),
        # Decodable text that fails JSON parsing is malformed TEXT, not binary.
        pytest.param(
            httpx.Response(200, headers={"content-type": "application/json"}, content=b"not valid json"),
            "HTTP/1.1 200 OK\ncontent-type: application/json\ncontent-length: 14\n\nnot valid json",
            id="malformed-json-as-text",
        ),
        # httpx's .json() raises UnicodeDecodeError (not only JSONDecodeError)
        # for undecodable bytes served as JSON; the formatter degrades to .text
        # like the carrier's equivalent call sites do.
        pytest.param(
            httpx.Response(200, headers={"content-type": "application/json"}, content=b"\xff\xfe\x00b\x00a\x00d"),
            "HTTP/1.1 200 OK\ncontent-type: application/json\ncontent-length: 8\n\n��\x00b\x00a\x00d",
            id="undecodable-json-as-text",
        ),
        # A non-textual content type must not dump (possibly mojibake) bytes
        # into the report; it emits a short placeholder instead.
        pytest.param(
            httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=b"\xff\xfe\x00\x01\x80\x81\x82"),
            "HTTP/1.1 200 OK\ncontent-type: application/octet-stream\ncontent-length: 7\n\n<Binary content: 7 bytes>",
            id="non-textual-placeholder",
        ),
        pytest.param(
            httpx.Response(204, headers={"content-type": "text/plain"}, content=b""),
            "HTTP/1.1 204 No Content\ncontent-type: text/plain\n",
            id="empty-body",
        ),
        # A response whose transport recorded no HTTP version still gets a start line.
        pytest.param(
            httpx.Response(200, headers={"content-type": "text/plain"}, content=b"OK", extensions={"http_version": b""}),
            "HTTP/1.1 200 OK\ncontent-type: text/plain\ncontent-length: 2\n\nOK",
            id="missing-http-version",
        ),
    ],
)
def test_format_response(response, expected):
    assert format_response(response) == expected


@pytest.mark.parametrize(
    ("formatter", "message", "expected_body"),
    [
        pytest.param(
            format_request,
            httpx.Request("POST", "https://x.test/", headers={"content-type": "text/plain"}, content=b"x" * 2000),
            "x" * 1000 + "... (truncated)",
            id="text-request",
        ),
        pytest.param(format_request, httpx.Request("POST", "https://x.test/", json=_BIG_JSON), json.dumps(_BIG_JSON, indent=2)[:1000] + "... (truncated)", id="json-request"),
        pytest.param(format_response, httpx.Response(200, json=_BIG_JSON), json.dumps(_BIG_JSON, indent=2)[:1000] + "... (truncated)", id="json-response"),
    ],
)
def test_long_bodies_are_truncated(formatter, message, expected_body):
    """Bodies are capped at 1000 characters, and the pretty-printed JSON
    branches honor the cap too, not only plain text."""
    assert formatter(message).split("\n\n", 1)[1] == expected_body


@pytest.mark.parametrize(
    ("formatter", "message", "start_line"),
    [
        pytest.param(format_request, httpx.Request("POST", "https://x.test/", headers=_JSON, content=TOO_DEEP_TO_PARSE), "POST https://x.test/\nhost: x.test", id="request"),
        pytest.param(format_response, httpx.Response(200, headers=_JSON, content=TOO_DEEP_TO_PARSE), "HTTP/1.1 200 OK", id="response"),
    ],
)
def test_json_too_deep_to_parse_shows_as_text(formatter, message, start_line):
    """Like malformed JSON, rather than an error placeholder that drops the
    start line and headers too: the decoder raises RecursionError, which is
    not a ValueError."""
    expected = f"{start_line}\ncontent-type: application/json\ncontent-length: 2000000\n\n" + "[" * 1000 + "... (truncated)"
    assert on_bounded_stack(formatter, message) == expected


@pytest.mark.parametrize(
    ("formatter", "message"),
    [
        pytest.param(format_request, httpx.Request("POST", "https://x.test/", headers=_JSON, content=_DEEP_JSON), id="request"),
        pytest.param(format_response, httpx.Response(200, headers=_JSON, content=_DEEP_JSON), id="response"),
    ],
)
def test_deep_json_renders_only_what_is_shown(formatter, message):
    """Indentation grows with depth, so a deep body pretty-prints in full to a
    size quadratic in its own, only to be cut to 1,000 characters. Rendering
    must stop at the cap (the whole parsed body is about 0.2 MB)."""
    tracemalloc.start()
    try:
        body = formatter(message).split("\n\n", 1)[1]
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert body == "\n".join("  " * level + "[" for level in range(40))[:1000] + "... (truncated)"
    assert peak < 2_000_000
