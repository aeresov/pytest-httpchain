import json
import shlex
import tracemalloc
from urllib.parse import quote

import httpx
import msgpack
import pytest

from pytest_httpchain.redaction import DEFAULT_REDACTION, NO_REDACTION
from pytest_httpchain.report_formatter import format_curl, format_request, format_response
from tests.unit.helpers import TOO_DEEP_TO_PARSE, on_bounded_stack

_UNDECODABLE = bytes(range(256))
_BIG_JSON = {"data": ["x" * 50] * 200}
_JSON = {"content-type": "application/json"}
# Parses on every supported interpreter, but pretty-prints in full to 12.5 MB.
_DEEP_JSON = b"[" * 2_500 + b"]" * 2_500
# A multipart body, as the plugin sends one: a field, then a binary file.
_MULTIPART = (
    b'--XyZ\r\nContent-Disposition: form-data; name="title"\r\n\r\nReport\r\n'
    b'--XyZ\r\nContent-Disposition: form-data; name="image"; filename="a.png"\r\nContent-Type: image/png\r\n\r\n\x89PNG\r\n'
    b"--XyZ--\r\n"
)


def test_messagepack_report_shows_binary_values_readably():
    payload = msgpack.packb({"b": b"\x00\xff", "name": "device"}, use_bin_type=True)
    request = httpx.Request("POST", "https://example.com/", content=payload, headers={"content-type": "application/msgpack"})
    response = httpx.Response(200, content=payload, headers={"content-type": "application/octet-stream"})
    response.extensions["httpchain_response_codec"] = "msgpack"
    assert '"b": "<bytes: 00ff>"' in format_request(request)
    assert '"b": "<bytes: 00ff>"' in format_response(response)


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        pytest.param(
            httpx.Request("GET", "https://example.com/api/users", params={"page": "1", "limit": "10"}, headers={"authorization": "Bearer token123", "x-custom": "value"}),
            # Redacted by default: the ini defaults apply without configuration.
            "GET https://example.com/api/users?page=1&limit=10\nhost: example.com\nauthorization: [REDACTED]\nx-custom: value\n",
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
        # Bodies are shown as sent: `password` is redacted in a query, not here.
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
            "POST https://example.com/upload\nhost: example.com\ntransfer-encoding: chunked\n\n<Streaming body: not captured>",
            id="streaming-placeholder",
        ),
        # A multipart body part by part: a binary file stands in for itself
        # alone, where the whole body was one <Binary content>.
        pytest.param(
            httpx.Request("POST", "https://example.com/upload", headers={"content-type": "multipart/form-data; boundary=XyZ"}, content=_MULTIPART),
            "POST https://example.com/upload\nhost: example.com\ncontent-type: multipart/form-data; boundary=XyZ\ncontent-length: 176\n\n"
            '--XyZ\nContent-Disposition: form-data; name="title"\n\nReport\n'
            '--XyZ\nContent-Disposition: form-data; name="image"; filename="a.png"\nContent-Type: image/png\n\n<Binary content: 4 bytes>\n'
            "--XyZ--",
            id="multipart-part-by-part",
        ),
        pytest.param(
            httpx.Request("POST", "https://example.com/upload", headers={"content-type": "multipart/form-data; boundary=XyZ"}, content=b"--XyZ--\r\n"),
            "POST https://example.com/upload\nhost: example.com\ncontent-type: multipart/form-data; boundary=XyZ\ncontent-length: 9\n\n--XyZ--",
            id="multipart-without-parts",
        ),
        # Delimited by another boundary than its header names (a stage's own
        # Content-Type can): shown as any body is.
        pytest.param(
            httpx.Request("POST", "https://example.com/upload", headers={"content-type": "multipart/form-data; boundary=other"}, content=_MULTIPART),
            "POST https://example.com/upload\nhost: example.com\ncontent-type: multipart/form-data; boundary=other\ncontent-length: 176\n\n<Binary content: 176 bytes>",
            id="multipart-other-boundary",
        ),
        # Not a multipart body after all: cut short before its closing
        # delimiter, or a part without the empty line ending its headers.
        *(
            pytest.param(
                httpx.Request("POST", "https://example.com/upload", headers={"content-type": "multipart/form-data; boundary=XyZ"}, content=content),
                f"POST https://example.com/upload\nhost: example.com\ncontent-type: multipart/form-data; boundary=XyZ\ncontent-length: {len(content)}\n\n{content.decode()}",
                id=f"multipart-malformed-{name}",
            )
            for name, content in [("unclosed", b'--XyZ\r\nContent-Disposition: form-data; name="a"\r\n\r\nx'), ("no-headers-end", b"--XyZ\r\nx\r\n--XyZ--\r\n")]
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
    ("formatter", "message"),
    [
        (format_request, httpx.Request("POST", "https://x.test/", headers={"content-type": "Application/JSON"}, content=b'{"a":1}')),
        (format_response, httpx.Response(200, headers={"content-type": "Application/JSON"}, content=b'{"a":1}')),
    ],
)
def test_json_content_type_is_case_insensitive(formatter, message):
    assert formatter(message).split("\n\n", 1)[1] == '{\n  "a": 1\n}'


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


_CREDENTIALED_REQUEST = httpx.Request(
    "GET",
    "https://u:pw@x.test/p?page=1&access_token=q-secret",
    headers={"authorization": "Bearer h-secret", "cookie": "sid=c-secret; theme=dark", "x-trace": "t1"},
)
_CREDENTIALED_RESPONSE = httpx.Response(
    302,
    headers=[("set-cookie", "sid=s-secret; Path=/; HttpOnly"), ("set-cookie", "csrf=k-secret; Path=/"), ("location", "/cb?state=s&token=l-secret")],
)


@pytest.mark.parametrize(
    ("redaction", "expected_request", "expected_response"),
    [
        pytest.param(
            DEFAULT_REDACTION,
            "GET https://u:[REDACTED]@x.test/p?page=1&access_token=[REDACTED]\nhost: x.test\nauthorization: [REDACTED]\ncookie: sid=[REDACTED]; theme=[REDACTED]\nx-trace: t1\n",
            "HTTP/1.1 302 Found\nset-cookie: sid=[REDACTED]; Path=/; HttpOnly\nset-cookie: csrf=[REDACTED]; Path=/\nlocation: /cb?state=s&token=[REDACTED]\n",
            id="default",
        ),
        pytest.param(
            NO_REDACTION,
            "GET https://u:pw@x.test/p?page=1&access_token=q-secret\nhost: x.test\nauthorization: Bearer h-secret\ncookie: sid=c-secret; theme=dark\nx-trace: t1\n",
            "HTTP/1.1 302 Found\nset-cookie: sid=s-secret; Path=/; HttpOnly\nset-cookie: csrf=k-secret; Path=/\nlocation: /cb?state=s&token=l-secret\n",
            id="disabled",
        ),
    ],
)
def test_redaction_covers_start_line_and_headers(redaction, expected_request, expected_response):
    """Names stay visible, values go: the request's URL (query and userinfo
    password) and headers, and the response's headers, a redirect's Location
    included."""
    assert format_request(_CREDENTIALED_REQUEST, redaction) == expected_request
    assert format_response(_CREDENTIALED_RESPONSE, redaction) == expected_response


def _lines(*lines: str) -> str:
    return "\n".join(lines)


def _curl_arguments(command: str) -> list[str]:
    """What a POSIX shell hands curl for ``command``, comment lines dropped."""
    return shlex.split(command.replace(" \\\n", " "), comments=True)


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        pytest.param(
            _CREDENTIALED_REQUEST,
            # [REDACTED] in the URL would be a curl glob range: --globoff.
            _lines(
                "# [REDACTED] stands for a value this report hides: fill it in before running.",
                "curl -X GET 'https://u:[REDACTED]@x.test/p?page=1&access_token=[REDACTED]' \\",
                "  --globoff \\",
                "  -H 'authorization: [REDACTED]' \\",
                "  -H 'cookie: sid=[REDACTED]; theme=[REDACTED]' \\",
                "  -H 'x-trace: t1'",
            ),
            id="redacted",
        ),
        pytest.param(
            httpx.Request("GET", "https://x.test/p", headers={"x-trace": "t1"}),
            _lines(
                "curl -X GET 'https://x.test/p' \\",
                "  -H 'x-trace: t1'",
            ),
            id="nothing-redacted-no-comment",
        ),
        pytest.param(
            httpx.Request(
                "DELETE",
                "https://x.test/items?filter[id]=1",
                headers=[("connection", "keep-alive"), ("accept-encoding", "gzip, deflate"), ("x-empty", ""), ("x-multi", "a"), ("x-multi", "b")],
            ),
            # curl writes Host and Connection itself, and httpx's own
            # Accept-Encoding with --compressed; `Name;` sends an empty header
            # where `Name:` would remove it; a repeated header stays two lines.
            _lines(
                "curl -X DELETE 'https://x.test/items?filter[id]=1' \\",
                "  --globoff \\",
                "  -H 'x-empty;' \\",
                "  -H 'x-multi: a' \\",
                "  -H 'x-multi: b' \\",
                "  --compressed",
            ),
            id="header-filtering",
        ),
        pytest.param(
            httpx.Request("GET", "https://x.test:8443/", headers={"host": "vhost.test"}),
            # A Host the request set itself is not the URL's: sent as set.
            _lines(
                "curl -X GET 'https://x.test:8443/' \\",
                "  -H 'host: vhost.test'",
            ),
            id="virtual-host",
        ),
        # -X HEAD makes curl wait for a body that never comes, but --head
        # refuses to send one.
        pytest.param(httpx.Request("HEAD", "https://x.test/"), "curl --head 'https://x.test/'", id="head"),
        pytest.param(
            httpx.Request("HEAD", "https://x.test/", headers={"content-type": "text/plain"}, content=b"x"),
            _lines(
                "curl -X HEAD 'https://x.test/' \\",
                "  -H 'content-type: text/plain' \\",
                "  --data-raw 'x'",
            ),
            id="head-with-body",
        ),
        pytest.param(
            httpx.Request("POST", "https://x.test/api", headers={"content-type": "application/json"}, content=b'{"name": "Alice"}'),
            # As sent, not pretty-printed as the request section shows it.
            _lines(
                "curl -X POST 'https://x.test/api' \\",
                "  -H 'content-type: application/json' \\",
                """  --data-raw '{"name": "Alice"}'""",
            ),
            id="json",
        ),
        pytest.param(
            httpx.Request("POST", "https://x.test/login", data={"user": "alice", "password": "s3cret"}),
            # Bodies are shown as sent, in the command too.
            _lines(
                "curl -X POST 'https://x.test/login' \\",
                "  -H 'content-type: application/x-www-form-urlencoded' \\",
                "  --data-raw 'user=alice&password=s3cret'",
            ),
            id="form",
        ),
        pytest.param(
            httpx.Request("PUT", "https://x.test/raw", content=b"plain"),
            # Given a body without a Content-Type, curl would label it a form.
            _lines(
                "curl -X PUT 'https://x.test/raw' \\",
                "  -H 'Content-Type:' \\",
                "  --data-raw 'plain'",
            ),
            id="no-content-type",
        ),
        pytest.param(
            httpx.Request("POST", "https://x.test/upload", headers={"content-type": "application/octet-stream"}, content=_UNDECODABLE),
            _lines(
                "# The body is 256 bytes of binary data: save it as body.bin to send it.",
                "curl -X POST 'https://x.test/upload' \\",
                "  -H 'content-type: application/octet-stream' \\",
                "  --data-binary @body.bin",
            ),
            id="binary",
        ),
        pytest.param(
            # Valid UTF-8, but a NUL ends a C string, and so a shell argument.
            httpx.Request("POST", "https://x.test/upload", headers={"content-type": "text/plain"}, content=b"a\x00b"),
            _lines(
                "# The body is 3 bytes of binary data: save it as body.bin to send it.",
                "curl -X POST 'https://x.test/upload' \\",
                "  -H 'content-type: text/plain' \\",
                "  --data-binary @body.bin",
            ),
            id="nul-is-binary",
        ),
        pytest.param(
            # Cut short like the request section's, it would send another body.
            httpx.Request("POST", "https://x.test/bulk", headers={"content-type": "text/plain"}, content=b"x" * 10_001),
            _lines(
                "# The body, 10001 characters, is too long to show: save it as body.txt to send it.",
                "curl -X POST 'https://x.test/bulk' \\",
                "  -H 'content-type: text/plain' \\",
                "  --data-binary @body.txt",
            ),
            id="too-long",
        ),
        pytest.param(
            # The plugin's multipart body is bytes, sent as any other: with
            # the Content-Type naming its boundary.
            httpx.Request("POST", "https://x.test/upload", headers={"content-type": "multipart/form-data; boundary=XyZ"}, content=b"--XyZ--\r\n"),
            _lines(
                "curl -X POST 'https://x.test/upload' \\",
                "  -H 'content-type: multipart/form-data; boundary=XyZ' \\",
                # Its line breaks are CRLF, as sent.
                "  --data-raw '--XyZ--\r",
                "'",
            ),
            id="multipart-captured",
        ),
        pytest.param(
            # Its Content-Type's boundary belongs to the uncaptured body: -F writes its own.
            httpx.Request("POST", "https://x.test/upload", files={"file": ("a.txt", b"abc")}),
            _lines(
                "# The multipart body was not captured: add each part with -F 'name=@file'.",
                "curl -X POST 'https://x.test/upload'",
            ),
            id="multipart",
        ),
        pytest.param(
            httpx.Request("POST", "https://x.test/upload", content=iter([b"chunk"])),
            _lines(
                "# The streamed body was not captured: add it with --data-binary @file.",
                "curl -X POST 'https://x.test/upload'",
            ),
            id="streaming",
        ),
        pytest.param(
            httpx.Request("POST", "https://x.test/empty"),
            # httpx sends a bodyless POST's Content-Length: 0, and curl, given
            # no body, would send none: some servers answer that 411.
            _lines(
                "curl -X POST 'https://x.test/empty' \\",
                "  -H 'content-length: 0'",
            ),
            id="empty-body",
        ),
        pytest.param(
            httpx.Request("GET", "https://x.test/p", headers={"authorization": 'Digest username="u", nonce="n1", response="r1"', "x-trace": "t1"}),
            # Computed from one challenge's nonce, it would not answer another:
            # left out, not a [REDACTED] to fill in.
            _lines(
                "# The Digest Authorization answered one challenge and is left out: add --digest -u 'user:password' to answer a new one.",
                "curl -X GET 'https://x.test/p' \\",
                "  -H 'x-trace: t1'",
            ),
            id="digest-auth",
        ),
    ],
)
def test_format_curl(request_, expected):
    assert format_curl(request_) == expected


def test_format_curl_recognizes_multipart_media_type_case_insensitively():
    request = httpx.Request("POST", "https://x.test/upload", files={"file": ("a.txt", b"abc")})
    request.headers["content-type"] = request.headers["content-type"].replace("multipart/", "Multipart/")

    assert format_curl(request) == _lines(
        "# The multipart body was not captured: add each part with -F 'name=@file'.",
        "curl -X POST 'https://x.test/upload'",
    )


@pytest.mark.parametrize(
    ("value", "kept"),
    [
        # httpx's own, whichever of br and zstd it lists: curl asks for what
        # it decodes.
        pytest.param("gzip, deflate", False, id="httpx-default"),
        pytest.param("gzip, deflate, br, zstd", False, id="httpx-default-all-decoders"),
        pytest.param("GZIP,deflate", False, id="default-spelled-otherwise"),
        # Set on purpose: sent as set, which curl sends in place of its own.
        pytest.param("identity", True, id="identity"),
        pytest.param("br", True, id="one-coding"),
        pytest.param("gzip", True, id="gzip-alone"),
        pytest.param("", True, id="empty"),
        pytest.param("gzip;q=1.0, identity; q=0.5", True, id="weighted"),
        pytest.param("gzip, deflate, compress", True, id="coding-httpx-does-not-send"),
        pytest.param("gzip, gzip, deflate", True, id="repeated-coding"),
    ],
)
def test_format_curl_keeps_an_accept_encoding_the_request_set(value, kept):
    """--compressed is there either way: httpx decodes the answer whatever it
    asked for, curl only with it. Without a header of its own, curl asks for
    every coding it decodes, which a request for ``identity`` refused."""
    arguments = _curl_arguments(format_curl(httpx.Request("GET", "https://x.test/", headers={"accept-encoding": value})))
    header = ["-H", f"accept-encoding: {value}" if value else "accept-encoding;"] if kept else []
    assert arguments == ["curl", "-X", "GET", "https://x.test/", *header, "--compressed"]


def test_format_curl_without_redaction_shows_the_values():
    assert format_curl(_CREDENTIALED_REQUEST, NO_REDACTION) == _lines(
        "curl -X GET 'https://u:pw@x.test/p?page=1&access_token=q-secret' \\",
        "  -H 'authorization: Bearer h-secret' \\",
        "  -H 'cookie: sid=c-secret; theme=dark' \\",
        "  -H 'x-trace: t1'",
    )


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("it's", id="single-quote"),
        pytest.param('say "hi"', id="double-quotes"),
        pytest.param("line 1\nline 2\r\n\ttabbed", id="newlines"),
        pytest.param("Zoë 日本 🙂", id="unicode"),
        pytest.param("$HOME `id` $(id) \\n !! *", id="shell-syntax"),
        pytest.param("'", id="only-a-quote"),
        pytest.param("-d", id="looks-like-an-option"),
    ],
)
def test_format_curl_quotes_every_value_as_one_shell_word(text):
    """A shell hands curl each value as it was sent: quotes, newlines, non-ASCII
    text and shell syntax neither end the word nor run anything."""
    # A header value as bytes: httpx takes str values as ASCII only.
    request = httpx.Request("POST", "https://x.test/p?q=" + quote(text), headers={"content-type": "text/plain", "x-value": text.encode()}, content=text.encode())
    arguments = _curl_arguments(format_curl(request, NO_REDACTION))
    assert arguments == ["curl", "-X", "POST", str(request.url), "-H", "content-type: text/plain", "-H", f"x-value: {text}", "--data-raw", text]


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


def _multipart_request(*parts: tuple[bytes, bytes], closing: bytes = b"--XyZ--\r\n") -> httpx.Request:
    """A request whose body is ``parts``, each its headers and content, delimited by XyZ."""
    content = b"".join(b"--XyZ\r\n" + headers + b"\r\n\r\n" + data + b"\r\n" for headers, data in parts) + closing
    return httpx.Request("POST", "https://x.test/", headers={"content-type": "multipart/form-data; boundary=XyZ"}, content=content)


_CHUNK = 1 << 20  # report_formatter._DECODE_CHUNK


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        # Text throughout, but for its last byte: binary, as a body would be,
        # though the report shows none of the text past its first chunk.
        pytest.param(_multipart_request((b"H: 1", b"t" * (2 * _CHUNK) + b"\xff")), f"--XyZ\nH: 1\n\n<Binary content: {2 * _CHUNK + 1} bytes>\n--XyZ--", id="binary-at-its-end"),
        # A character split between two chunks is still one character.
        pytest.param(_multipart_request((b"H: 1", b"t" * (_CHUNK - 1) + "é".encode())), ("--XyZ\nH: 1\n\n" + "t" * 1000)[:1000] + "... (truncated)", id="character-across-chunks"),
        pytest.param(_multipart_request((b"H: 1", "é".encode() * 10)), "--XyZ\nH: 1\n\n" + "é" * 10 + "\n--XyZ--", id="multibyte-text"),
        # Malformed past what the report shows (a part without the empty line
        # ending its headers): not multipart, shown as any body is.
        pytest.param(
            _multipart_request((b"H: 1", b"t" * 2000), closing=b"--XyZ\r\nx\r\n--XyZ--\r\n"),
            ("--XyZ\r\nH: 1\r\n\r\n" + "t" * 2000)[:1000] + "... (truncated)",
            id="malformed-past-what-is-shown",
        ),
        # Parts past what is shown are found, not decoded, and not shown.
        pytest.param(
            _multipart_request((b"H: 1", b"t" * 2000), (b"H: 2", b"\xff" * 10)),
            ("--XyZ\nH: 1\n\n" + "t" * 2000)[:1000] + "... (truncated)",
            id="part-past-what-is-shown",
        ),
        # Headers are shown as far as the report shows them.
        pytest.param(_multipart_request((b"H: " + "é".encode() * 3000, b"x")), ("--XyZ\nH: " + "é" * 3000)[:1000] + "... (truncated)", id="long-headers"),
    ],
)
def test_format_request_multipart_body(request_, expected):
    assert format_request(request_).split("\n\n", 1)[1] == expected


def test_large_multipart_body_is_decoded_only_as_far_as_it_is_shown():
    """A failing stage's report shows 1,000 characters of a body, so a large
    upload is found through part by part and decoded a chunk at a time,
    keeping only what is shown: split and decoded whole, the 16 MB here was
    copied four times over."""
    request = _multipart_request((b'Content-Disposition: form-data; name="a"', b"t" * 8_000_000), (b'Content-Disposition: form-data; name="b"', b"\xff" * 8_000_000))
    tracemalloc.start()
    try:
        body = format_request(request).split("\n\n", 1)[1]
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert body == ('--XyZ\nContent-Disposition: form-data; name="a"\n\n' + "t" * 1000)[:1000] + "... (truncated)"
    assert peak < 4 * _CHUNK
