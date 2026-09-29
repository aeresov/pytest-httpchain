"""A HAR file's entries as `RecordedRequest`s, for ``import har``.

One request per entry, in the file's order, less those filtered out:

- a static asset, unless ``keep_all``: an entry whose response is an image,
  a stylesheet, a font or a script, by its MIME type (or, for an entry a
  browser recorded without one, a revalidated ``304``, by the resource type
  it recorded: Chrome writes the type ``x-unknown`` then), and that the page
  did not fetch itself (an XHR or fetch request, which is kept whatever it
  fetched);
- an entry whose URL matches no ``include`` pattern, when there are any, or
  matches an ``exclude`` one (``re.search``);
- one that is not http(s) (``data:``, ``ws:``), and one without a response
  (status 0: aborted, blocked), which has no status to verify.

A cookie a request sent because an earlier response set it is left out: the
scenario's client keeps the cookies its responses set, as the browser did,
and sends them itself. Only the entries imported count, since the client sees
only their responses. The client sends none of them for a request with a
Cookie header of its own, so a request that sent both kinds sends only the
client's, and a note names the others.
"""

import json
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlencode, urlsplit

from pytest_httpchain.importers.builder import (
    Base64Data,
    ImportSourceError,
    Part,
    PartsData,
    RecordedBody,
    RecordedRequest,
    TextData,
    media_type,
    parse_cookie_header,
)

# Browsers' resource types for what a page loads to render itself (Chrome's
# `_resourceType`), and those the page's own code fetches.
_STATIC_RESOURCE_TYPES = frozenset({"image", "stylesheet", "font", "script"})
_FETCHED_RESOURCE_TYPES = frozenset({"xhr", "fetch"})
_SCRIPT_TYPES = frozenset(
    {"application/javascript", "application/x-javascript", "application/ecmascript", "application/x-ecmascript", "text/javascript", "text/ecmascript", "text/jscript"}
)


def _is_static(entry: dict[str, Any]) -> bool:
    """Whether an entry fetched an image, a stylesheet, a font or a script
    for the page to render, rather than for its code (see the module
    docstring)."""
    resource_type = entry.get("_resourceType") if isinstance(entry.get("_resourceType"), str) else None
    if resource_type in _FETCHED_RESOURCE_TYPES:
        return False
    content = entry.get("response", {}).get("content")
    mime = media_type(content.get("mimeType") if isinstance(content, dict) and isinstance(content.get("mimeType"), str) else None)
    # Chrome writes "x-unknown" for a response without a type (a 304).
    if mime and mime != "x-unknown":
        return mime.startswith(("image/", "font/", "application/font-", "application/x-font-")) or mime in ("text/css", "application/vnd.ms-fontobject") or mime in _SCRIPT_TYPES
    return resource_type in _STATIC_RESOURCE_TYPES


def _pairs(items: Any) -> list[tuple[str, str]]:
    """A HAR list of ``{"name", "value"}`` records as pairs, skipping any
    that is not one."""
    if not isinstance(items, list):
        return []
    return [(item["name"], item["value"]) for item in items if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("value"), str)]


def _set_cookies(response: dict[str, Any]) -> list[tuple[str, str]]:
    """The cookies a response set: its cookie list, else its Set-Cookie headers."""
    if cookies := _pairs(response.get("cookies")):
        return cookies
    return [
        (name.strip(), value.strip())
        for header, line in _pairs(response.get("headers"))
        if header.lower() == "set-cookie"
        for name, equals, value in [line.partition(";")[0].partition("=")]
        if equals
    ]


def _body(post_data: Any, notes: list[str], where: str) -> RecordedBody | None:
    """An entry's ``postData`` as the body sent. Text as recorded, or with
    ``"encoding": "base64"`` (the plugin's own export, for bytes that are
    not text) bytes; a multipart body recorded as its parts (``params``) part
    by part; a form recorded as its params alone encoded again."""
    if not isinstance(post_data, dict):
        return None
    text = post_data.get("text")
    params = post_data.get("params")
    mime = media_type(post_data.get("mimeType") if isinstance(post_data.get("mimeType"), str) else None)
    if isinstance(text, str) and text:
        return Base64Data(text) if post_data.get("encoding") == "base64" else TextData(text)
    if isinstance(params, list) and params:
        if mime == "multipart/form-data":
            parts = []
            for param in params:
                if not isinstance(param, dict) or not isinstance(param.get("name"), str):
                    continue
                value = param.get("value") if isinstance(param.get("value"), str) else None
                filename = param.get("fileName") if isinstance(param.get("fileName"), str) else None
                content_type = param.get("contentType") if isinstance(param.get("contentType"), str) and param.get("contentType") else None
                if filename is None:
                    parts.append(Part(param["name"], value=value or "", content_type=content_type))
                    continue
                if value is None:
                    notes.append(f"{where}: the file {filename!r} of part {param['name']!r} was not recorded, and is sent empty: fill it in")
                parts.append(Part(param["name"], content=value or "", filename=filename, content_type=content_type))
            return PartsData(tuple(parts))
        return TextData(urlencode(_pairs(params)))
    return None


def _entry_request(entry: dict[str, Any], jar: dict[str, str], notes: list[str], where: str) -> RecordedRequest:
    request = entry["request"]
    headers: list[tuple[str, str]] = []
    cookies: list[tuple[str, str]] = []
    for name, value in _pairs(request.get("headers")):
        if name.lower() == "cookie":
            cookies += parse_cookie_header(value)
        else:
            headers.append((name, value))
    if not cookies:
        cookies = _pairs(request.get("cookies"))
    set_earlier = [name for name, value in cookies if jar.get(name) == value]
    if set_earlier:
        # A Cookie header of the scenario's own would keep the client from
        # sending the cookies its responses set, so those win.
        if others := [name for name, value in cookies if jar.get(name) != value]:
            notes.append(
                f"{where}: left out the cookies no earlier response set ({', '.join(others)}): in a Cookie header, they would keep the client"
                f" from sending those its responses set ({', '.join(set_earlier)})"
            )
        cookies = []
    return RecordedRequest(
        method=request["method"].upper(),
        url=request["url"],
        headers=headers,
        cookies=cookies,
        body=_body(request.get("postData"), notes, where),
        # Each redirect is an entry of its own.
        follow_redirects=False,
        status=entry["response"]["status"],
    )


def read_har(
    text: str,
    *,
    source: str,
    keep_all: bool = False,
    include: Sequence[re.Pattern[str]] = (),
    exclude: Sequence[re.Pattern[str]] = (),
) -> tuple[list[RecordedRequest], list[str]]:
    """The requests of a HAR file's entries (see the module docstring), and
    notes saying what was left out. ``source`` names the file in messages.

    Raises `ImportSourceError` for a file that is no HAR (not JSON, no
    ``log.entries``, an entry without a request's method and URL) and for
    one that leaves nothing to import once filtered.
    """
    try:
        har = json.loads(text)
    except (ValueError, RecursionError) as e:
        raise ImportSourceError(f"{source} is not a HAR file: it is not JSON ({e})") from None
    entries = har.get("log", {}).get("entries") if isinstance(har, dict) and isinstance(har.get("log"), dict) else None
    if not isinstance(entries, list):
        raise ImportSourceError(f"{source} is not a HAR file: it has no log.entries list")

    notes: list[str] = []
    requests: list[RecordedRequest] = []
    skipped = {"scheme": 0, "response": 0, "static": 0, "filter": 0}
    jar: dict[str, str] = {}
    for number, entry in enumerate(entries, start=1):
        request = entry.get("request") if isinstance(entry, dict) else None
        if not isinstance(request, dict) or not isinstance(request.get("method"), str) or not isinstance(request.get("url"), str):
            raise ImportSourceError(f"{source} is not a HAR file: its entry {number} has no request with a method and a URL")
        url = request["url"]
        response = entry.get("response")
        try:
            scheme = urlsplit(url).scheme
        except ValueError as e:
            raise ImportSourceError(f"{source} is not a HAR file: its entry {number} has the URL {url!r}, which is no URL ({e})") from None
        if scheme.lower() not in ("http", "https"):
            skipped["scheme"] += 1
        elif not isinstance(response, dict) or not isinstance(response.get("status"), int) or not 100 <= response["status"] <= 599:
            skipped["response"] += 1
        elif not keep_all and _is_static(entry):
            skipped["static"] += 1
        elif (include and not any(pattern.search(url) for pattern in include)) or any(pattern.search(url) for pattern in exclude):
            skipped["filter"] += 1
        else:
            requests.append(_entry_request(entry, jar, notes, f"Entry {number} ({request['method']} {url})"))
            jar.update(_set_cookies(response))

    for kind, what in (
        ("static", "static {}: images, stylesheets, fonts and scripts the page loaded (--all keeps them)"),
        ("filter", "{} by --include/--exclude"),
        ("response", "{} without a response (status 0: aborted or blocked), with no status to verify"),
        ("scheme", "{} with a URL other than http or https"),
    ):
        if count := skipped[kind]:
            noun = ("asset" if count == 1 else "assets") if kind == "static" else ("entry" if count == 1 else "entries")
            notes.append(f"Skipped {count} " + what.format(noun))
    if not requests:
        raise ImportSourceError(f"{source} has no entries to import" + (f" ({'; '.join(notes)})" if notes else ""))
    return requests, notes
