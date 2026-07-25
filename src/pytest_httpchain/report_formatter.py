import json

import httpx

# Maximum number of characters of a request/response body to include in a report
# before truncating. Shared by both format_request and format_response.
_MAX_BODY_CHARS = 1000


def _is_textual_content_type(content_type: str) -> bool:
    """Return True if the Content-Type looks like text we can safely display."""
    ct = content_type.lower()
    return ct.startswith("text/") or "json" in ct or "xml" in ct or "x-www-form-urlencoded" in ct


def _message_lines(start_line: str, headers: httpx.Headers, body: str | None) -> str:
    """Assemble one HTTP message: start line, headers, blank line, optional body.

    Shared by request and response formatting — only the start line and the
    body-rendering rules differ between the two.
    """
    lines = [start_line, *(f"{name}: {value}" for name, value in headers.items()), ""]
    if body is not None:
        lines.append(body)
    return "\n".join(lines)


def format_request(request: httpx.Request) -> str:
    """Format an httpx Request for display."""
    body = None
    if request.content:
        try:
            decoded = request.content.decode()
        except UnicodeDecodeError:
            # Genuinely undecodable bytes: only here is the binary label correct.
            body = f"<Binary content: {len(request.content)} bytes>"
        else:
            # Decoded fine. Pretty-print JSON when it parses; a JSON body that
            # fails to parse is malformed *text*, not binary — show it as text.
            # The pretty-printed form goes through the same truncation cap.
            if "application/json" in request.headers.get("content-type", ""):
                try:
                    decoded = json.dumps(json.loads(decoded), indent=2, ensure_ascii=False)
                except json.JSONDecodeError:
                    pass
            body = _format_body_text(decoded)

    return _message_lines(f"{request.method} {request.url}", request.headers, body)


def format_response(response: httpx.Response) -> str:
    """Format an httpx Response for display."""
    body = None
    if response.content:
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                body = _format_body_text(json.dumps(response.json(), indent=2, ensure_ascii=False))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # httpx's .json() raises UnicodeDecodeError (not only
                # JSONDecodeError) for undecodable bytes served as JSON —
                # mirror the carrier's equivalent call sites.
                body = _format_body_text(response.text)
        elif _is_textual_content_type(content_type):
            body = _format_body_text(response.text)
        else:
            # Non-textual (or unknown) content type: avoid dumping mojibake.
            body = f"<binary {len(response.content)} bytes>"

    http_version = response.http_version or "HTTP/1.1"
    return _message_lines(f"{http_version} {response.status_code} {response.reason_phrase}", response.headers, body)


def _format_body_text(text: str) -> str:
    """Truncate a decoded body to the shared maximum length for display."""
    if len(text) > _MAX_BODY_CHARS:
        return f"{text[:_MAX_BODY_CHARS]}... (truncated)"
    return text
