import json

import httpx

# Maximum number of characters of a request/response body to include in a report
# before truncating. Shared by both format_request and format_response.
_MAX_BODY_CHARS = 1000


def _is_textual_content_type(content_type: str) -> bool:
    """Return True if the Content-Type looks like text we can safely display."""
    ct = content_type.lower()
    return ct.startswith("text/") or "json" in ct or "xml" in ct or "x-www-form-urlencoded" in ct


def format_request(request: httpx.Request) -> str:
    """Format an httpx Request for display."""
    lines = []

    # Request line
    lines.append(f"{request.method} {request.url}")

    # Headers
    for name, value in request.headers.items():
        lines.append(f"{name}: {value}")

    # Empty line between headers and body
    lines.append("")

    # Body
    if request.content:
        content_type = request.headers.get("content-type", "")
        try:
            decoded = request.content.decode()
        except UnicodeDecodeError:
            # Genuinely undecodable bytes: only here is the binary label correct.
            lines.append(f"<Binary content: {len(request.content)} bytes>")
        else:
            # Decoded fine. Pretty-print JSON when it parses; a JSON body that
            # fails to parse is malformed *text*, not binary — show it as text.
            # The pretty-printed form goes through the same truncation cap.
            if "application/json" in content_type:
                try:
                    lines.append(_format_body_text(json.dumps(json.loads(decoded), indent=2, ensure_ascii=False)))
                except json.JSONDecodeError:
                    lines.append(_format_body_text(decoded))
            else:
                lines.append(_format_body_text(decoded))

    return "\n".join(lines)


def format_response(response: httpx.Response) -> str:
    """Format an httpx Response for display."""
    lines = []

    # Status line
    http_version = response.http_version if response.http_version else "HTTP/1.1"
    lines.append(f"{http_version} {response.status_code} {response.reason_phrase}")

    # Headers
    for key, value in response.headers.items():
        lines.append(f"{key}: {value}")

    # Empty line between headers and body
    lines.append("")

    # Body
    if response.content:
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                lines.append(_format_body_text(json.dumps(response.json(), indent=2, ensure_ascii=False)))
            except json.JSONDecodeError:
                lines.append(_format_body_text(response.text))
        elif _is_textual_content_type(content_type):
            lines.append(_format_body_text(response.text))
        else:
            # Non-textual (or unknown) content type: avoid dumping mojibake.
            lines.append(f"<binary {len(response.content)} bytes>")

    return "\n".join(lines)


def _format_body_text(text: str) -> str:
    """Truncate a decoded body to the shared maximum length for display."""
    if len(text) > _MAX_BODY_CHARS:
        return f"{text[:_MAX_BODY_CHARS]}... (truncated)"
    return text
