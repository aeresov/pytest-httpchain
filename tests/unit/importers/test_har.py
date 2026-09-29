"""Unit tests for importers/har.py: a HAR file's entries as the requests a
scenario sends again, and the entries left out."""

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from pytest_httpchain.har_writer import create_har_log, request_response_to_har_entry
from pytest_httpchain.importers import ImportSourceError, Part, PartsData, TextData, build_scenario, read_har
from pytest_httpchain.models import Request
from pytest_httpchain.request_builder import build_request_kwargs
from tests.unit.importers.helpers import comparable, sent_requests

EXAMPLE = Path(__file__).parent / "example.har"


def _har(*entries: dict[str, Any]) -> str:
    return json.dumps({"log": {"version": "1.2", "entries": list(entries)}})


def _entry(url: str, method: str = "GET", status: int = 200, **request: Any) -> dict[str, Any]:
    return {"request": {"method": method, "url": url, **request}, "response": {"status": status, "content": {"mimeType": "application/json"}}}


def test_example_har():
    """A browser's export: the page's own requests in order, each verifying
    the status it got, the static assets it loaded left out; the headers
    every request sends are the client's, a request's transport headers
    (HTTP/2's pseudo-headers, Accept-Encoding, Content-Length) and those its
    body form sets are gone, and each body is the form its type names."""
    requests, notes = read_har(EXAMPLE.read_text(), source="example.har")
    result = build_scenario(requests, description="Imported from example.har")
    scenario = result.scenario

    assert notes == [
        "Entry 8 (GET https://app.example.com/api/me): left out the cookies no earlier response set (prefs): in a Cookie header,"
        " they would keep the client from sending those its responses set (session)",
        "Skipped 5 static assets: images, stylesheets, fonts and scripts the page loaded (--all keeps them)",
        "Skipped 1 entry without a response (status 0: aborted or blocked), with no status to verify",
        "Skipped 1 entry with a URL other than http or https",
    ]
    assert scenario["client"] == {
        "base_url": "https://app.example.com",
        "headers": {"user-agent": "Mozilla/5.0 (X11; Linux x86_64) Example/1.0"},
        # Each redirect is an entry of its own, verified as recorded.
        "follow_redirects": False,
    }
    assert [(stage["name"], stage["response"]) for stage in scenario["stages"]] == [
        ("get_root", [{"verify": {"status": 200}}]),
        ("post_api_login", [{"verify": {"status": 200}}]),
        ("get_api_me", [{"verify": {"status": 200}}]),
        # An image the page's code fetched is the page's own request.
        ("get_api_avatar_1", [{"verify": {"status": 200}}]),
        ("post_api_search", [{"verify": {"status": 200}}]),
        ("post_api_photos", [{"verify": {"status": 201}}]),
        ("get_old_page", [{"verify": {"status": 302}}]),
    ]
    root, login, me, avatar, search, photos, _ = (stage["request"] for stage in scenario["stages"])
    assert root == {"url": "/", "headers": {"accept": "text/html", "Cookie": "{{ cookie }}"}}
    assert login == {"method": "POST", "url": "/api/login", "body": {"json": {"username": "demo", "password": "{{ password }}"}}}
    # The session cookie the login response set is the client's to send,
    # which a Cookie header would keep it from doing.
    assert me == {"url": "/api/me", "auth": {"bearer": "{{ api_token }}"}}
    assert avatar == {"url": "/api/avatar/1", "auth": {"bearer": "{{ api_token }}"}}
    # A query holding a secret stays in the URL, where its placeholder fails
    # the stage unset (params would send it empty).
    assert search == {"method": "POST", "url": "/api/search?page=2&api_key={{ quote(api_key) }}", "body": {"form": {"q": "red shoes", "size": ["42", "43"]}}}
    assert photos == {
        "method": "POST",
        "url": "/api/photos",
        "body": {"multipart": {"fields": {"title": "Holiday"}, "files": {"photo": {"content": "sun and sand", "filename": "beach.txt", "content_type": "text/plain"}}}},
    }
    assert scenario["substitutions"] == [
        {"vars": {"cookie": "{{ env('COOKIE') }}", "password": "{{ env('PASSWORD') }}", "api_token": "{{ env('API_TOKEN') }}", "api_key": "{{ env('API_KEY') }}"}}
    ]
    for secret in ("hunter2", "eyJ.token", "K3y", "dark", "s3ss10n"):
        assert secret not in json.dumps(scenario)


def test_all_keeps_the_static_assets():
    """``--all`` keeps what a page loaded to render itself, from another
    origin too, which leaves the scenario without one base URL."""
    requests, notes = read_har(EXAMPLE.read_text(), source="example.har", keep_all=True)
    scenario = build_scenario(requests, description="all").scenario
    assert len(scenario["stages"]) == 12
    assert "base_url" not in scenario["client"]
    assert scenario["stages"][4]["request"]["url"] == "https://fonts.example.net/inter.woff2"
    assert not any(note.startswith("Skipped 5 static") for note in notes)


@pytest.mark.parametrize(
    ("include", "exclude", "urls", "note"),
    [
        pytest.param(["/api/"], [], ["/api/login", "/api/me", "/api/avatar/1", "/api/search", "/api/photos"], "Skipped 2 entries by --include/--exclude", id="include"),
        pytest.param(["login", "photos"], [], ["/api/login", "/api/photos"], "Skipped 5 entries by --include/--exclude", id="include-any"),
        pytest.param(
            [],
            ["avatar", r"^https://app\.example\.com/$"],
            ["/api/login", "/api/me", "/api/search", "/api/photos", "/old-page"],
            "Skipped 2 entries by --include/--exclude",
            id="exclude",
        ),
        pytest.param(["/api/"], ["search"], ["/api/login", "/api/me", "/api/avatar/1", "/api/photos"], "Skipped 3 entries by --include/--exclude", id="both"),
    ],
)
def test_include_and_exclude(include, exclude, urls, note):
    requests, notes = read_har(EXAMPLE.read_text(), source="example.har", include=[re.compile(p) for p in include], exclude=[re.compile(p) for p in exclude])
    assert [httpx.URL(request.url).path for request in requests] == urls
    assert note in notes


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param("not json", "x.har is not a HAR file: it is not JSON (Expecting value: line 1 column 1 (char 0))", id="not-json"),
        pytest.param("[]", "x.har is not a HAR file: it has no log.entries list", id="not-an-object"),
        pytest.param('{"log": {}}', "x.har is not a HAR file: it has no log.entries list", id="no-entries"),
        pytest.param(_har({"request": {"url": "https://x.test/"}}), "x.har is not a HAR file: its entry 1 has no request with a method and a URL", id="entry-without-method"),
        pytest.param(_har(), "x.har has no entries to import", id="empty"),
        pytest.param(_har(_entry("http://[::1/x")), "x.har is not a HAR file: its entry 1 has the URL 'http://[::1/x', which is no URL (Invalid IPv6 URL)", id="malformed-url"),
        pytest.param(
            _har(_entry("https://x.test/logo.png") | {"response": {"status": 200, "content": {"mimeType": "image/png"}}}),
            "x.har has no entries to import (Skipped 1 static asset: images, stylesheets, fonts and scripts the page loaded (--all keeps them))",
            id="all-filtered",
        ),
    ],
)
def test_what_is_no_har_fails_cleanly(text, message):
    with pytest.raises(ImportSourceError, match=f"^{re.escape(message)}$"):
        read_har(text, source="x.har")


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"_resourceType": ["xhr"]}, id="resource-type-list"),
        pytest.param({"_resourceType": {"type": "xhr"}}, id="resource-type-object"),
        pytest.param({"response": {"status": 200, "content": ["image/png"]}}, id="content-list"),
        pytest.param({"response": {"status": 200, "content": {"mimeType": 5}, "headers": "x", "cookies": [1, {"name": "a"}]}}, id="response-fields"),
    ],
)
def test_fields_of_another_type_are_ignored(fields):
    """A HAR file from some other tool may give a field another type than
    the format's: read as absent, never a traceback."""
    entry = _entry("https://x.test/a") | fields
    [request], _ = read_har(_har(entry), source="x.har")
    assert request.url == "https://x.test/a"


@pytest.mark.parametrize(
    ("resource_type", "mime_type", "kept"),
    [
        # The response's MIME type decides...
        pytest.param(None, "image/png", False, id="image"),
        pytest.param(None, "text/css; charset=utf-8", False, id="stylesheet"),
        pytest.param(None, "font/woff2", False, id="font"),
        pytest.param(None, "text/javascript", False, id="script"),
        pytest.param("script", "application/json", True, id="type-over-resource-type"),
        # ... unless none was recorded (a revalidated 304): "" or, as Chrome
        # writes it, "x-unknown". Then the resource type does.
        pytest.param("script", "x-unknown", False, id="chrome-304-script"),
        pytest.param("image", "", False, id="304-image"),
        pytest.param("document", "x-unknown", True, id="chrome-304-document"),
        pytest.param(None, "x-unknown", True, id="nothing-recorded"),
        # What the page's code fetched is its own, whatever it is.
        pytest.param("xhr", "image/png", True, id="xhr-image"),
        pytest.param("fetch", "x-unknown", True, id="fetch"),
    ],
)
def test_static_assets(resource_type, mime_type, kept):
    entry = _entry("https://x.test/a", status=304) | {"response": {"status": 304, "content": {"mimeType": mime_type}}}
    if resource_type is not None:
        entry["_resourceType"] = resource_type
    if kept:
        [request], _ = read_har(_har(entry), source="x.har")
        assert request.url == "https://x.test/a"
    else:
        with pytest.raises(ImportSourceError, match=re.escape("(Skipped 1 static asset: ")):
            read_har(_har(entry), source="x.har")


def test_entry_with_an_invalid_status_is_skipped():
    """A status outside 100-599 is no status to verify, as a missing one is."""
    requests, notes = read_har(_har(_entry("https://x.test/a", status=999), _entry("https://x.test/b")), source="x.har")
    assert [request.url for request in requests] == ["https://x.test/b"]
    assert notes == ["Skipped 1 entry without a response (status 0: aborted or blocked), with no status to verify"]


def test_multipart_recorded_as_parts():
    """A multipart body recorded part by part (``params``, as Firefox writes
    it) is sent as those parts; a file whose content was not recorded is
    sent empty, and a note says to fill it in."""
    params = [
        {"name": "title", "value": "Hi"},
        {"name": "doc", "fileName": "a.txt", "contentType": "text/plain", "value": "hello"},
        {"name": "photo", "fileName": "b.png", "contentType": "image/png"},
    ]
    har = _har(_entry("https://x.test/up", "POST", postData={"mimeType": "multipart/form-data; boundary=x", "params": params}))
    [request], notes = read_har(har, source="x.har")
    assert request.body == PartsData(
        (
            Part("title", value="Hi"),
            Part("doc", content="hello", filename="a.txt", content_type="text/plain"),
            Part("photo", content="", filename="b.png", content_type="image/png"),
        )
    )
    assert notes == ["Entry 1 (POST https://x.test/up): the file 'b.png' of part 'photo' was not recorded, and is sent empty: fill it in"]


@pytest.mark.parametrize(
    "post_data",
    [
        pytest.param({"mimeType": "text/plain"}, id="neither-text-nor-params"),
        pytest.param({"mimeType": "text/plain", "text": ""}, id="empty-text"),
        pytest.param("not an object", id="not-an-object"),
    ],
)
def test_entry_without_a_body(post_data):
    [request], _ = read_har(_har(_entry("https://x.test/a", "POST", postData=post_data)), source="x.har")
    assert request.body is None


def test_multipart_params_that_are_no_part_are_skipped():
    har = _har(_entry("https://x.test/up", "POST", postData={"mimeType": "multipart/form-data", "params": [{"value": "nameless"}, "junk", {"name": "a", "value": "1"}]}))
    [request], _ = read_har(har, source="x.har")
    assert request.body == PartsData((Part("a", value="1"),))


def test_form_recorded_as_params_only():
    har = _har(
        _entry("https://x.test/f", "POST", postData={"mimeType": "application/x-www-form-urlencoded", "params": [{"name": "a", "value": "1 2"}, {"name": "b", "value": "&"}]})
    )
    [request], _ = read_har(har, source="x.har")
    assert request.body == TextData("a=1+2&b=%26")


@pytest.mark.parametrize(
    ("request_fields", "response_fields", "cookies", "notes"),
    [
        # A cookie list stands in for a missing Cookie header.
        pytest.param({"cookies": [{"name": "a", "value": "1"}]}, {}, [("a", "1")], [], id="cookie-list"),
        # One an earlier response set is the client's to send, by Set-Cookie
        # header or cookie list.
        pytest.param({"headers": [{"name": "Cookie", "value": "b=2"}]}, {"headers": [{"name": "Set-Cookie", "value": "b=2; Path=/"}]}, [], [], id="set-cookie-header"),
        pytest.param({"cookies": [{"name": "b", "value": "2"}]}, {"cookies": [{"name": "b", "value": "2"}]}, [], [], id="set-cookie-list"),
        # Only as long as it has the value the response set.
        pytest.param({"headers": [{"name": "Cookie", "value": "b=3"}]}, {"cookies": [{"name": "b", "value": "2"}]}, [("b", "3")], [], id="changed-value"),
        # A Cookie header would keep the client from sending the one it keeps.
        pytest.param(
            {"headers": [{"name": "Cookie", "value": "a=1; b=2"}]},
            {"cookies": [{"name": "b", "value": "2"}]},
            [],
            [
                "Entry 2 (GET https://x.test/me): left out the cookies no earlier response set (a):"
                " in a Cookie header, they would keep the client from sending those its responses set (b)"
            ],
            id="both-kinds",
        ),
    ],
)
def test_cookies(request_fields, response_fields, cookies, notes):
    first = _entry("https://x.test/login") | {"response": {"status": 200, **response_fields}}
    [_, second], found = read_har(_har(first, _entry("https://x.test/me", **request_fields)), source="x.har")
    assert second.cookies == cookies
    assert found == notes


def test_cookie_set_by_a_skipped_entry_is_still_sent():
    """The scenario's client sees only the responses of the entries it
    sends: a cookie a left-out entry set is sent as recorded."""
    skipped = {
        "request": {"method": "GET", "url": "https://x.test/logo.png"},
        "response": {"status": 200, "content": {"mimeType": "image/png"}, "cookies": [{"name": "a", "value": "1"}]},
    }
    [request], _ = read_har(_har(skipped, _entry("https://x.test/me", cookies=[{"name": "a", "value": "1"}])), source="x.har")
    assert request.cookies == [("a", "1")]


def _plugin_request(body: dict[str, Any] | None, **fields: Any) -> httpx.Request:
    request = Request.model_validate({"url": "https://api.test/x", "method": "POST", **({"body": body} if body else {}), **fields})
    return httpx.Client().build_request(**build_request_kwargs(request))


@pytest.mark.parametrize(
    "original",
    [
        pytest.param(_plugin_request({"json": {"a": [1, "{{ b }}"]}}), id="json"),
        pytest.param(_plugin_request({"form": {"a": "1", "b": ["x", "y"]}}), id="form"),
        pytest.param(_plugin_request({"text": "hello"}, headers={"Content-Type": "text/plain"}), id="text"),
        # Bytes that are not text: base64, with "encoding" set.
        pytest.param(_plugin_request({"base64": "AAEC/w=="}), id="binary"),
        pytest.param(_plugin_request({"multipart": {"fields": {"t": "x"}, "files": {"f": {"base64": "AAEC/w==", "filename": "b.bin"}}}}), id="multipart"),
        pytest.param(_plugin_request(None, method="GET", params={"q": "a b"}, headers={"X-Trace": "1"}), id="query"),
    ],
)
def test_plugin_har_export_round_trips(original):
    """The HAR the plugin exports (``--httpchain-output-dir``) imports as a
    scenario sending its requests again."""
    har = create_har_log([request_response_to_har_entry(original, httpx.Response(200, request=original))])
    requests, _ = read_har(json.dumps(har), source="export.har")
    [sent] = sent_requests(build_scenario(requests, description="export").scenario)
    assert comparable(sent) == comparable(original)
