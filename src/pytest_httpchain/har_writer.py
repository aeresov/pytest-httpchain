"""HAR (HTTP Archive) format writer for pytest-httpchain.

This module converts httpx Request/Response objects to HAR 1.2 format
and writes them to files for external analysis.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx


def _get_version() -> str:
    """Get pytest-httpchain version for HAR creator info."""
    try:
        from importlib.metadata import version

        return version("pytest-httpchain")
    except Exception:
        return "unknown"


def _format_cookies(cookies: httpx.Cookies) -> list[dict[str, Any]]:
    """Convert httpx Cookies to HAR cookie format."""
    result = []
    for name, value in cookies.items():
        result.append({"name": name, "value": value})
    return result


def _parse_cookie_header(cookie_header: str) -> list[dict[str, str]]:
    """Parse Cookie header string into HAR cookie format."""
    if not cookie_header:
        return []
    result = []
    for pair in cookie_header.split(";"):
        pair = pair.strip()
        if "=" in pair:
            name, value = pair.split("=", 1)
            result.append({"name": name.strip(), "value": value.strip()})
    return result


def _format_headers(headers: httpx.Headers) -> list[dict[str, str]]:
    """Convert httpx Headers to HAR header format."""
    return [{"name": name, "value": value} for name, value in headers.items()]


def _format_query_string(url: httpx.URL) -> list[dict[str, str]]:
    """Extract query string parameters from URL."""
    parsed = urlparse(str(url))
    params = parse_qs(parsed.query, keep_blank_values=True)
    result = []
    for name, values in params.items():
        for value in values:
            result.append({"name": name, "value": value})
    return result


def _format_post_data(request: httpx.Request) -> dict[str, Any] | None:
    """Format request body as HAR postData."""
    if not request.content:
        return None

    content_type = request.headers.get("content-type", "")
    mime_type = content_type.split(";")[0].strip() if content_type else "application/octet-stream"

    try:
        text = request.content.decode("utf-8")
    except UnicodeDecodeError:
        import base64

        text = base64.b64encode(request.content).decode("ascii")
        return {
            "mimeType": mime_type,
            "text": text,
            "encoding": "base64",
        }

    post_data: dict[str, Any] = {
        "mimeType": mime_type,
        "text": text,
    }

    if "application/x-www-form-urlencoded" in content_type:
        params = parse_qs(text, keep_blank_values=True)
        post_data["params"] = [{"name": k, "value": v[0] if len(v) == 1 else v} for k, v in params.items()]

    return post_data


def _format_response_content(response: httpx.Response) -> dict[str, Any]:
    """Format response body as HAR content."""
    content_type = response.headers.get("content-type", "")
    mime_type = content_type.split(";")[0].strip() if content_type else "application/octet-stream"

    content: dict[str, Any] = {
        "size": len(response.content) if response.content else 0,
        "mimeType": mime_type,
    }

    if response.content:
        try:
            content["text"] = response.content.decode("utf-8")
        except UnicodeDecodeError:
            import base64

            content["text"] = base64.b64encode(response.content).decode("ascii")
            content["encoding"] = "base64"

    return content


def _calculate_headers_size(headers: httpx.Headers) -> int:
    """Calculate approximate size of headers in bytes."""
    size = 0
    for name, value in headers.items():
        size += len(name) + len(value) + 4
    return size


def request_response_to_har_entry(
    request: httpx.Request,
    response: httpx.Response,
    started_datetime: datetime | None = None,
    elapsed_ms: float = 0,
) -> dict[str, Any]:
    """Convert an httpx Request/Response pair to a HAR entry.

    Args:
        request: The httpx Request object.
        response: The httpx Response object.
        started_datetime: When the request started (defaults to now).
        elapsed_ms: Total elapsed time in milliseconds.

    Returns:
        A dictionary representing a HAR entry.
    """
    if started_datetime is None:
        started_datetime = datetime.now(UTC)

    entry: dict[str, Any] = {
        "startedDateTime": started_datetime.isoformat(),
        "time": elapsed_ms,
        "request": {
            "method": request.method,
            "url": str(request.url),
            "httpVersion": response.http_version or "HTTP/1.1",
            "cookies": _parse_cookie_header(request.headers.get("cookie", "")),
            "headers": _format_headers(request.headers),
            "queryString": _format_query_string(request.url),
            "headersSize": _calculate_headers_size(request.headers),
            "bodySize": len(request.content) if request.content else 0,
        },
        "response": {
            "status": response.status_code,
            "statusText": response.reason_phrase or "",
            "httpVersion": response.http_version or "HTTP/1.1",
            "cookies": _format_cookies(response.cookies),
            "headers": _format_headers(response.headers),
            "content": _format_response_content(response),
            "redirectURL": response.headers.get("location", ""),
            "headersSize": _calculate_headers_size(response.headers),
            "bodySize": len(response.content) if response.content else 0,
        },
        "cache": {},
        "timings": {
            "send": -1,
            "wait": elapsed_ms if elapsed_ms > 0 else -1,
            "receive": -1,
        },
    }

    post_data = _format_post_data(request)
    if post_data:
        entry["request"]["postData"] = post_data

    return entry


def create_har_log(entries: list[dict[str, Any]], comment: str | None = None) -> dict[str, Any]:
    """Create a complete HAR log structure.

    Args:
        entries: List of HAR entry dictionaries.
        comment: Optional comment to include in the log.

    Returns:
        A complete HAR log dictionary.
    """
    har: dict[str, Any] = {
        "log": {
            "version": "1.2",
            "creator": {
                "name": "pytest-httpchain",
                "version": _get_version(),
            },
            "entries": entries,
        }
    }

    if comment:
        har["log"]["comment"] = comment

    return har


def write_har_file(
    output_dir: Path,
    test_name: str,
    request: httpx.Request,
    response: httpx.Response,
    started_datetime: datetime | None = None,
    elapsed_ms: float = 0,
) -> Path:
    """Write a HAR file for a single test.

    Args:
        output_dir: Directory to write the HAR file to.
        test_name: Name of the test (used for filename).
        request: The httpx Request object.
        response: The httpx Response object.
        started_datetime: When the request started.
        elapsed_ms: Total elapsed time in milliseconds.

    Returns:
        Path to the written HAR file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_name = test_name.replace("/", "_").replace("\\", "_").replace(":", "_")
    filename = f"{safe_name}.har"
    filepath = output_dir / filename

    entry = request_response_to_har_entry(request, response, started_datetime, elapsed_ms)
    har = create_har_log([entry], comment=f"Test: {test_name}")

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(har, f, indent=2, ensure_ascii=False)

    return filepath
