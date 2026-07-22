import json

import httpx
import pytest

from pytest_httpchain.report_formatter import format_request, format_response


class TestFormatRequest:
    def test_simple_get_request(self):
        request = httpx.Request("GET", "https://example.com/api/users")
        result = format_request(request)

        assert "GET https://example.com/api/users" in result
        assert "host: example.com" in result

    def test_request_with_json_body(self):
        request = httpx.Request(
            "POST",
            "https://example.com/api/users",
            headers={"content-type": "application/json"},
            json={"name": "Alice", "age": 30},
        )
        result = format_request(request)

        assert "POST https://example.com/api/users" in result
        assert '"name": "Alice"' in result
        assert '"age": 30' in result

    def test_request_with_non_json_body(self):
        request = httpx.Request(
            "POST",
            "https://example.com/api/data",
            headers={"content-type": "text/plain"},
            content=b"Hello, World!",
        )
        result = format_request(request)

        assert "POST https://example.com/api/data" in result
        assert "Hello, World!" in result

    def test_request_with_form_body(self):
        request = httpx.Request(
            "POST",
            "https://example.com/api/login",
            data={"username": "alice", "password": "secret"},
        )
        result = format_request(request)

        assert "POST https://example.com/api/login" in result
        assert "username=alice" in result
        assert "password=secret" in result

    def test_request_with_binary_content(self):
        # Genuinely undecodable bytes: only these earn the binary label.
        binary_data = bytes(range(256))
        with pytest.raises(UnicodeDecodeError):
            binary_data.decode()
        request = httpx.Request(
            "POST",
            "https://example.com/api/upload",
            headers={"content-type": "application/octet-stream"},
            content=binary_data,
        )
        result = format_request(request)

        assert "POST https://example.com/api/upload" in result
        assert "<Binary content: 256 bytes>" in result

    def test_request_with_malformed_json_shown_as_text(self):
        # content-type says JSON but the body fails to parse. It still decodes as
        # text, so it must be displayed as text — NOT mislabeled binary.
        request = httpx.Request(
            "POST",
            "https://example.com/api/data",
            headers={"content-type": "application/json"},
            content=b"{not valid json",
        )
        result = format_request(request)

        assert "POST https://example.com/api/data" in result
        assert "{not valid json" in result
        assert "Binary content" not in result

    def test_request_with_long_body_truncated(self):
        long_content = "x" * 2000
        request = httpx.Request(
            "POST",
            "https://example.com/api/data",
            headers={"content-type": "text/plain"},
            content=long_content.encode(),
        )
        result = format_request(request)

        assert "... (truncated)" in result
        assert len(result) < 2000 + 500  # truncated + headers overhead

    def test_request_with_empty_body(self):
        request = httpx.Request("GET", "https://example.com/api/users")
        result = format_request(request)

        # Should still format without error
        assert "GET https://example.com/api/users" in result

    def test_request_with_headers(self):
        request = httpx.Request(
            "GET",
            "https://example.com/api/users",
            headers={"authorization": "Bearer token123", "x-custom": "value"},
        )
        result = format_request(request)

        assert "authorization: Bearer token123" in result
        assert "x-custom: value" in result

    def test_request_with_query_params(self):
        request = httpx.Request(
            "GET",
            "https://example.com/api/users",
            params={"page": "1", "limit": "10"},
        )
        result = format_request(request)

        assert "page=1" in result
        assert "limit=10" in result


class TestFormatResponse:
    def test_simple_response(self):
        response = httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"OK",
        )
        result = format_response(response)

        assert "200" in result
        assert "OK" in result

    def test_response_with_json_body(self):
        json_data = {"id": 1, "name": "Alice"}
        response = httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(json_data).encode(),
        )
        result = format_response(response)

        assert "200" in result
        assert '"id": 1' in result
        assert '"name": "Alice"' in result

    def test_response_with_invalid_json(self):
        # A body that decodes as text but fails JSON parsing is malformed TEXT,
        # not binary: show the text, never the binary placeholder.
        response = httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b"not valid json",
        )
        result = format_response(response)

        assert "200" in result
        assert "not valid json" in result
        assert "binary" not in result.lower()

    def test_response_with_non_textual_content_type(self):
        # A non-textual content type must not dump (possibly mojibake) bytes
        # into the report; it emits a short placeholder instead.
        binary_data = b"\xff\xfe\x00\x01" + bytes([0x80, 0x81, 0x82])
        response = httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=binary_data,
        )
        result = format_response(response)

        assert "200" in result
        assert f"<binary {len(binary_data)} bytes>" in result

    def test_response_with_binary_content_uses_placeholder(self):
        # Genuinely undecodable bytes under a non-textual content type: the
        # report must show the binary placeholder, never the raw (mojibake) bytes.
        binary_data = bytes(range(256))
        # Sanity-check that these bytes really do not decode as UTF-8, so the
        # placeholder assertion below is meaningful rather than incidental.
        with pytest.raises(UnicodeDecodeError):
            binary_data.decode()
        response = httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=binary_data,
        )
        result = format_response(response)

        assert "200" in result
        assert f"<binary {len(binary_data)} bytes>" in result

    def test_response_with_headers(self):
        response = httpx.Response(
            200,
            headers={
                "content-type": "text/plain",
                "x-request-id": "abc123",
                "cache-control": "no-cache",
            },
            content=b"OK",
        )
        result = format_response(response)

        assert "x-request-id: abc123" in result
        assert "cache-control: no-cache" in result

    def test_response_404_not_found(self):
        response = httpx.Response(
            404,
            headers={"content-type": "application/json"},
            content=b'{"error": "Not found"}',
        )
        result = format_response(response)

        assert "404" in result
        assert '"error": "Not found"' in result

    def test_response_500_server_error(self):
        response = httpx.Response(
            500,
            headers={"content-type": "text/html"},
            content=b"<html><body>Internal Server Error</body></html>",
        )
        result = format_response(response)

        assert "500" in result
        assert "Internal Server Error" in result

    def test_response_empty_body(self):
        response = httpx.Response(
            204,
            headers={"content-type": "text/plain"},
            content=b"",
        )
        result = format_response(response)

        assert "204" in result

    def test_response_http_version_fallback(self):
        response = httpx.Response(
            200,
            content=b"OK",
        )
        result = format_response(response)

        # Should have HTTP version (defaults to HTTP/1.1)
        assert "HTTP" in result


class TestJsonBodyTruncation:
    """_MAX_BODY_CHARS promises request/response bodies are capped; the
    pretty-printed JSON branches must honor it too, not only plain text."""

    def test_large_json_request_body_truncated(self):
        big = {"data": ["x" * 50] * 200}
        request = httpx.Request("POST", "https://x.test/", json=big)
        out = format_request(request)
        assert "(truncated)" in out
        assert len(out) < 3000

    def test_large_json_response_body_truncated(self):
        big = {"data": ["x" * 50] * 200}
        response = httpx.Response(200, json=big)
        out = format_response(response)
        assert "(truncated)" in out
        assert len(out) < 3000
