import json
from typing import Any

import httpx

from pytest_httpchain.utils import JSON_PARSE_ERRORS, request_content

_MAX_BODY_CHARS = 1000

_PRETTY_JSON = json.JSONEncoder(indent=2, ensure_ascii=False)


def _is_textual_content_type(content_type: str) -> bool:
    """True when the Content-Type looks like text we can safely display."""
    ct = content_type.lower()
    return ct.startswith("text/") or "json" in ct or "xml" in ct or "x-www-form-urlencoded" in ct


def _message_lines(start_line: str, headers: httpx.Headers, body: str | None) -> str:
    """Assemble one HTTP message: start line, headers, blank line, optional body."""
    # multi_items() so a repeated header (notably Set-Cookie, which must never be
    # comma-folded) prints as the separate wire lines it was sent as.
    lines = [start_line, *(f"{name}: {value}" for name, value in headers.multi_items()), ""]
    if body is not None:
        lines.append(body)
    return "\n".join(lines)


def format_request(request: httpx.Request) -> str:
    """Format an httpx Request for display."""
    content = request_content(request)
    body = None
    if content is None:
        body = "<Streaming body (e.g. multipart file upload): consumed on send, not captured>"
    elif content:
        try:
            decoded = content.decode()
        except UnicodeDecodeError:
            body = f"<Binary content: {len(content)} bytes>"
        else:
            body = _format_body_text(decoded)
            if "application/json" in request.headers.get("content-type", ""):
                try:
                    body = _format_json(json.loads(decoded))
                except JSON_PARSE_ERRORS:
                    # A JSON body that fails to parse is malformed text, not binary.
                    pass

    return _message_lines(f"{request.method} {request.url}", request.headers, body)


def format_response(response: httpx.Response) -> str:
    """Format an httpx Response for display."""
    body = None
    if response.content:
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                body = _format_json(response.json())
            except JSON_PARSE_ERRORS:
                # Undecodable or too deeply nested bodies show as text too.
                body = _format_body_text(response.text)
        elif _is_textual_content_type(content_type):
            body = _format_body_text(response.text)
        else:
            body = f"<Binary content: {len(response.content)} bytes>"

    http_version = response.http_version or "HTTP/1.1"
    return _message_lines(f"{http_version} {response.status_code} {response.reason_phrase}", response.headers, body)


def _format_json(value: Any) -> str:
    """``value`` pretty-printed, rendering only as much as the report shows.

    Indentation grows with depth, so a deeply nested body renders in full to
    a size quadratic in its own (a 10 kB body 5,000 levels deep renders to
    50 MB) only to be cut to ``_MAX_BODY_CHARS``. The encoder streams, so stop
    once past the cap.
    """
    chunks: list[str] = []
    length = 0
    for chunk in _PRETTY_JSON.iterencode(value):
        chunks.append(chunk)
        length += len(chunk)
        if length > _MAX_BODY_CHARS:
            break
    return _format_body_text("".join(chunks))


def _format_body_text(text: str) -> str:
    if len(text) > _MAX_BODY_CHARS:
        return f"{text[:_MAX_BODY_CHARS]}... (truncated)"
    return text
