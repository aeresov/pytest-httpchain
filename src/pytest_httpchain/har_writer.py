"""HAR 1.2 export of a test's httpx request/response pairs."""

import base64
import functools
import hashlib
import json
import re
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlparse

import httpx

from pytest_httpchain.utils import request_content

# One wire exchange: the request, its response (None when none arrived), and when
# it went on the wire (None falls back to HAR write time).
type Exchange = tuple[httpx.Request, httpx.Response | None, datetime | None]


@functools.cache
def _get_version() -> str:
    """Cached: the lookup scans installed-distribution metadata, which cannot
    change in-process."""
    try:
        return version("pytest-httpchain")
    except Exception:
        return "unknown"


def _format_cookies(cookies: httpx.Cookies) -> list[dict[str, Any]]:
    """Serialize jar entries without collapsing cookies by name.

    ``httpx.Cookies.items()`` performs a name-only lookup and raises
    ``CookieConflict`` when the same name exists at different paths/domains — a
    valid and common response. Iterating the jar preserves each scoped cookie.
    """
    result: list[dict[str, Any]] = []
    for cookie in cookies.jar:
        item: dict[str, Any] = {
            "name": cookie.name,
            "value": cookie.value or "",
            "secure": bool(cookie.secure),
            "httpOnly": cookie.has_nonstandard_attr("HttpOnly"),
        }
        if cookie.path:
            item["path"] = cookie.path
        if cookie.domain:
            item["domain"] = cookie.domain
        if cookie.expires is not None:
            item["expires"] = datetime.fromtimestamp(cookie.expires, UTC).isoformat()
        result.append(item)
    return result


def _parse_cookie_header(cookie_header: str) -> list[dict[str, str]]:
    # An empty header splits to [""], which carries no "=" and so yields nothing.
    pairs = (pair.partition("=") for pair in cookie_header.split(";"))
    return [{"name": name.strip(), "value": value.strip()} for name, separator, value in pairs if separator]


def _format_headers(headers: httpx.Headers) -> list[dict[str, str]]:
    # multi_items(), not items(): the latter comma-folds repeated names, which
    # RFC 6265 forbids for Set-Cookie precisely because cookie attributes
    # contain commas — folding two cookies corrupts both.
    return [{"name": name, "value": value} for name, value in headers.multi_items()]


def _format_query_string(url: httpx.URL) -> list[dict[str, str]]:
    params = parse_qs(urlparse(str(url)).query, keep_blank_values=True)
    return [{"name": name, "value": value} for name, values in params.items() for value in values]


def _mime_type(content_type: str) -> str:
    return content_type.split(";")[0].strip() if content_type else "application/octet-stream"


def _body_text(content: bytes) -> dict[str, str]:
    """HAR ``text`` for a body: UTF-8 as is, anything else base64 with ``encoding`` set."""
    try:
        return {"text": content.decode("utf-8")}
    except UnicodeDecodeError:
        return {"text": base64.b64encode(content).decode("ascii"), "encoding": "base64"}


def _format_post_data(request: httpx.Request) -> dict[str, Any] | None:
    content = request_content(request)
    if not content:
        return None

    content_type = request.headers.get("content-type", "")
    body_text = _body_text(content)
    post_data: dict[str, Any] = {"mimeType": _mime_type(content_type), **body_text}

    if "application/x-www-form-urlencoded" in content_type and "encoding" not in body_text:
        # HAR params are repeated scalar name/value records, not one record with
        # an array value. parse_qsl also preserves the body's original ordering.
        post_data["params"] = [{"name": name, "value": value} for name, value in parse_qsl(body_text["text"], keep_blank_values=True)]

    return post_data


def _format_response_content(response: httpx.Response) -> dict[str, Any]:
    content: dict[str, Any] = {
        "size": len(response.content),
        "mimeType": _mime_type(response.headers.get("content-type", "")),
    }
    if response.content:
        content |= _body_text(response.content)
    return content


def _calculate_headers_size(headers: httpx.Headers) -> int:
    # ": " (2) + CRLF (2) per header line; multi_items() so repeated headers are
    # counted as the separate wire lines they are.
    return sum(len(name) + len(value) + 4 for name, value in headers.multi_items())


def _response_elapsed_ms(response: httpx.Response) -> float:
    """Elapsed milliseconds, or 0 when httpx has not recorded them (reading
    ``elapsed`` too early raises, and synthetic responses may lack it)."""
    try:
        elapsed = response.elapsed
    except (RuntimeError, AttributeError):
        return 0
    return elapsed.total_seconds() * 1000


def request_response_to_har_entry(
    request: httpx.Request,
    response: httpx.Response | None,
    started_datetime: datetime | None = None,
) -> dict[str, Any]:
    """One HAR entry for a request and its response.

    ``response`` is None when none was received (timeout, connection error); the
    entry then carries a synthesized ``status: 0`` response, as browser exports
    do for aborted requests.
    """
    if started_datetime is None:
        started_datetime = datetime.now(UTC)

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
            "bodySize": len(response.content),
        }
    else:
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

    # -1 is HAR's "unknown": a streaming (multipart) body was consumed on send
    # and its bytes are no longer available.
    content = request_content(request)
    body_size = -1 if content is None else len(content)

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
            "bodySize": body_size,
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
    """Wrap HAR entries in the log envelope."""
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


# A node ID carries path separators, "::", and whatever a parametrize id holds —
# "?", "*", quotes, control characters, all illegal in a Windows filename — so the
# safe set is enumerated instead of chased with a deny-list. Restricting it to
# ASCII also makes the cap below a byte count, which is what filesystems limit.
_UNSAFE_FILENAME_RUN = re.compile(r"[^A-Za-z0-9._-]+")

# Longest single path component ext4/APFS/NTFS accept. A deeply nested scenario
# path plus long parametrize ids reaches it, and an over-long name fails the write.
_MAX_FILENAME_BYTES = 255


def _har_filename(test_name: str) -> str:
    """HAR file name for a pytest node ID: safe on every filesystem, still readable."""
    stem = _UNSAFE_FILENAME_RUN.sub("_", test_name) or "har"
    # Both the substitution and the truncation below are lossy, so a digest of the
    # original node ID is what keeps distinct tests' files from overwriting each other.
    digest = f"-{hashlib.sha1(test_name.encode(), usedforsecurity=False).hexdigest()[:8]}"
    stem = stem[: _MAX_FILENAME_BYTES - len(digest) - len(".har")]
    return f"{stem}.har" if stem == test_name else f"{stem}{digest}.har"


def write_har_file(
    output_dir: Path,
    test_name: str,
    exchanges: list[Exchange],
) -> Path:
    """Write one test's exchanges to a HAR file and return its path.

    ``exchanges`` are ``(request, response, started)`` triples in execution
    order — one per iteration for a parallel stage. A ``started`` of None falls
    back to write time.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    filepath = output_dir / _har_filename(test_name)

    entries = [request_response_to_har_entry(request, response, started) for request, response, started in exchanges]
    har = create_har_log(entries, comment=f"Test: {test_name}")

    filepath.write_text(json.dumps(har, indent=2, ensure_ascii=False), encoding="utf-8")

    return filepath
