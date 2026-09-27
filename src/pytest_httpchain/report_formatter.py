"""HTTP request/response formatting for the report sections.

Header values and URL query values are shown through a `Redaction`, which
defaults to the ``httpchain_redact_*`` ini defaults; bodies are shown as sent.
"""

import json

import httpx

from pytest_httpchain.redaction import DEFAULT_REDACTION, Redaction
from pytest_httpchain.utils import request_content

_MAX_BODY_CHARS = 1000


def _is_textual_content_type(content_type: str) -> bool:
    """True when the Content-Type looks like text we can safely display."""
    ct = content_type.lower()
    return ct.startswith("text/") or "json" in ct or "xml" in ct or "x-www-form-urlencoded" in ct


def _message_lines(start_line: str, headers: httpx.Headers, body: str | None, redaction: Redaction) -> str:
    """Assemble one HTTP message: start line, headers, blank line, optional body."""
    lines = [start_line, *(f"{name}: {value}" for name, value in redaction.header_items(headers)), ""]
    if body is not None:
        lines.append(body)
    return "\n".join(lines)


def format_request(request: httpx.Request, redaction: Redaction = DEFAULT_REDACTION) -> str:
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
            # A JSON body that fails to parse is malformed text, not binary.
            if "application/json" in request.headers.get("content-type", ""):
                try:
                    decoded = json.dumps(json.loads(decoded), indent=2, ensure_ascii=False)
                except json.JSONDecodeError:
                    pass
            body = _format_body_text(decoded)

    return _message_lines(f"{request.method} {redaction.url(request.url)}", request.headers, body, redaction)


def format_response(response: httpx.Response, redaction: Redaction = DEFAULT_REDACTION) -> str:
    """Format an httpx Response for display."""
    body = None
    if response.content:
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                body = _format_body_text(json.dumps(response.json(), indent=2, ensure_ascii=False))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # .json() raises UnicodeDecodeError too, for undecodable bytes.
                body = _format_body_text(response.text)
        elif _is_textual_content_type(content_type):
            body = _format_body_text(response.text)
        else:
            body = f"<Binary content: {len(response.content)} bytes>"

    http_version = response.http_version or "HTTP/1.1"
    return _message_lines(f"{http_version} {response.status_code} {response.reason_phrase}", response.headers, body, redaction)


def _format_body_text(text: str) -> str:
    if len(text) > _MAX_BODY_CHARS:
        return f"{text[:_MAX_BODY_CHARS]}... (truncated)"
    return text
