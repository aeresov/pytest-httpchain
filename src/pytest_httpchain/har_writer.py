"""HAR (HTTP Archive) format writer for pytest-httpchain.

This module converts httpx Request/Response objects to HAR 1.2 format
and writes them to files for external analysis.
"""

import base64
import hashlib
import json
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx


def _get_version() -> str:
    """Get pytest-httpchain version for HAR creator info."""
    try:
        return version("pytest-httpchain")
    except Exception:
        return "unknown"


def _format_cookies(cookies: httpx.Cookies) -> list[dict[str, str]]:
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
            content["text"] = base64.b64encode(response.content).decode("ascii")
            content["encoding"] = "base64"

    return content


def _calculate_headers_size(headers: httpx.Headers) -> int:
    """Calculate approximate size of headers in bytes."""
    size = 0
    for name, value in headers.items():
        size += len(name) + len(value) + 4  # ": " (2) + CRLF (2) per header line
    return size


def _response_elapsed_ms(response: httpx.Response) -> float:
    """Return the response's elapsed time in milliseconds, or 0 if unavailable.

    httpx populates ``response.elapsed`` (a timedelta) once the response has been
    read; accessing it before then raises RuntimeError. Guard against that and
    against the attribute being absent on mock/synthetic responses.
    """
    try:
        elapsed = response.elapsed
    except (RuntimeError, AttributeError):
        return 0
    return elapsed.total_seconds() * 1000


def request_response_to_har_entry(
    request: httpx.Request,
    response: httpx.Response | None,
    started_datetime: datetime | None = None,
    elapsed_ms: float | None = None,
) -> dict[str, Any]:
    """Convert an httpx Request/Response pair to a HAR entry.

    Args:
        request: The httpx Request object.
        response: The httpx Response object, or ``None`` when no response was
            received (timeout, connection error). Following the convention
            browser HAR exports use for aborted requests, the entry then
            carries a synthesized response with ``status: 0``.
        started_datetime: When the request started (defaults to now).
        elapsed_ms: Total elapsed time in milliseconds. When ``None`` (the
            default), the value is derived from ``response.elapsed``.

    Returns:
        A dictionary representing a HAR entry.
    """
    if started_datetime is None:
        started_datetime = datetime.now(UTC)

    if elapsed_ms is None:
        elapsed_ms = _response_elapsed_ms(response) if response is not None else 0

    http_version = (response.http_version if response is not None else None) or "HTTP/1.1"

    if response is not None:
        response_har: dict[str, Any] = {
            "status": response.status_code,
            "statusText": response.reason_phrase or "",
            "httpVersion": http_version,
            "cookies": _format_cookies(response.cookies),
            "headers": _format_headers(response.headers),
            "content": _format_response_content(response),
            "redirectURL": response.headers.get("location", ""),
            "headersSize": _calculate_headers_size(response.headers),
            "bodySize": len(response.content) if response.content else 0,
        }
    else:
        # No response received: status 0 with empty fields, the same shape
        # browsers write for aborted/failed requests.
        response_har = {
            "status": 0,
            "statusText": "",
            "httpVersion": http_version,
            "cookies": [],
            "headers": [],
            "content": {"size": 0, "mimeType": "x-unknown"},
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": -1,
        }

    entry: dict[str, Any] = {
        "startedDateTime": started_datetime.isoformat(),
        "time": elapsed_ms,
        "request": {
            "method": request.method,
            "url": str(request.url),
            "httpVersion": http_version,
            "cookies": _parse_cookie_header(request.headers.get("cookie", "")),
            "headers": _format_headers(request.headers),
            "queryString": _format_query_string(request.url),
            "headersSize": _calculate_headers_size(request.headers),
            "bodySize": len(request.content) if request.content else 0,
        },
        "response": response_har,
        "cache": {},
        "timings": {
            "send": -1,
            "wait": elapsed_ms if elapsed_ms > 0 else -1,
            "receive": -1,
        },
    }

    if response is None:
        entry["comment"] = "No response received (request failed or timed out)"

    post_data = _format_post_data(request)
    if post_data:
        entry["request"]["postData"] = post_data  # ty: ignore[invalid-assignment]

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
    exchanges: list[tuple[httpx.Request, httpx.Response | None, datetime | None]],
    started_datetime: datetime | None = None,
    elapsed_ms: float | None = None,
) -> Path:
    """Write a HAR file for a single test.

    Args:
        output_dir: Directory to write the HAR file to.
        test_name: Name of the test (used for filename).
        exchanges: The test's HTTP exchanges in execution order — one
            ``(request, response, started)`` triple per exchange. A parallel
            stage contributes one exchange per iteration; a response is
            ``None`` when none was received (timeout, connection error);
            ``started`` is the request's actual start time (HAR waterfalls
            are built from ``startedDateTime``), or ``None`` when unknown —
            the entry then falls back to ``started_datetime``/write time.
        started_datetime: Fallback start time for exchanges without their own.
        elapsed_ms: Total elapsed time in milliseconds (applied to every
            entry). When ``None`` (the default), each entry's value is derived
            from its response's ``elapsed``.

    Returns:
        Path to the written HAR file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    safe_name = test_name.replace("/", "_").replace("\\", "_").replace(":", "_")
    if safe_name != test_name:
        # Sanitization is not injective ("t/x" and "t:x" both map to "t_x"):
        # a short digest of the original name keeps distinct tests' files
        # distinct instead of silently overwriting each other.
        safe_name = f"{safe_name}-{hashlib.sha1(test_name.encode()).hexdigest()[:8]}"
    filename = f"{safe_name}.har"
    filepath = output_dir / filename

    entries = [request_response_to_har_entry(request, response, started or started_datetime, elapsed_ms) for request, response, started in exchanges]
    har = create_har_log(entries, comment=f"Test: {test_name}")

    filepath.write_text(json.dumps(har, indent=2, ensure_ascii=False), encoding="utf-8")

    return filepath
