import json

import httpx


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
            if "application/json" in content_type:
                body_data = json.loads(request.content.decode())
                lines.append(json.dumps(body_data, indent=2, ensure_ascii=False))
            else:
                decoded = request.content.decode()
                # Truncate very long bodies
                if len(decoded) > 1000:
                    lines.append(f"{decoded[:1000]}... (truncated)")
                else:
                    lines.append(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            lines.append(f"<Binary content: {len(request.content)} bytes>")

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
                lines.append(json.dumps(response.json(), indent=2, ensure_ascii=False))
            except (json.JSONDecodeError, UnicodeDecodeError):
                lines.append(response.text)
        else:
            try:
                lines.append(response.text)
            except UnicodeDecodeError:
                lines.append(f"<Binary content: {len(response.content)} bytes>")

    return "\n".join(lines)
