import base64
import datetime
import importlib.metadata
import json
from pathlib import Path

import httpx
import pytest

from pytest_httpchain import har_writer
from pytest_httpchain.har_writer import request_response_to_har_entry, write_har_file


def _make_pair(elapsed_ms: float | None = 123.5) -> tuple[httpx.Request, httpx.Response]:
    """Build an in-process httpx Request/Response pair with a known body.

    When ``elapsed_ms`` is not None, ``response.elapsed`` is populated so the
    HAR timing fields can be derived from it (M19 regression guard).
    """
    request = httpx.Request(
        "POST",
        "https://example.com/api/users",
        headers={"content-type": "application/json"},
        json={"name": "Alice"},
    )
    response = httpx.Response(
        201,
        headers={"content-type": "application/json"},
        content=json.dumps({"id": 1, "name": "Alice"}).encode(),
        request=request,
    )
    if elapsed_ms is not None:
        response.elapsed = datetime.timedelta(milliseconds=elapsed_ms)
    return request, response


def _write_one(tmp_path: Path, test_name: str = "test_users", elapsed_ms: float | None = 123.5) -> Path:
    request, response = _make_pair(elapsed_ms)
    return write_har_file(tmp_path, test_name, [(request, response, None)])


def _entry(path: Path, index: int = 0) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["log"]["entries"][index]


class TestWriteHarFile:
    def test_log_envelope(self, tmp_path):
        log = json.loads(_write_one(tmp_path).read_text(encoding="utf-8"))["log"]

        assert {key: value for key, value in log.items() if key != "entries"} == {
            "version": "1.2",
            "creator": {"name": "pytest-httpchain", "version": importlib.metadata.version("pytest-httpchain")},
            "comment": "Test: test_users",
        }
        assert len(log["entries"]) == 1

    def test_unknown_version_when_metadata_is_missing(self, monkeypatch):
        def missing(_name):
            raise importlib.metadata.PackageNotFoundError

        monkeypatch.setattr(har_writer, "version", missing)
        har_writer._get_version.cache_clear()
        try:
            assert har_writer._get_version() == "unknown"
        finally:
            har_writer._get_version.cache_clear()

    def test_entry_request_and_response_fields(self, tmp_path):
        entry = _entry(_write_one(tmp_path))

        assert entry["request"]["method"] == "POST"
        assert entry["request"]["url"] == "https://example.com/api/users"
        assert entry["response"]["status"] == 201

    def test_timing_is_nonzero(self, tmp_path):
        # M19 regression guard: a real duration derived from response.elapsed
        # must appear instead of 0.
        entry = _entry(_write_one(tmp_path, elapsed_ms=123.5))

        assert entry["time"] == 123.5
        assert entry["timings"]["wait"] == 123.5

    def test_missing_elapsed_is_handled(self, tmp_path):
        # When response.elapsed is unavailable (unread response), timing falls
        # back to 0 and the wait to HAR's "unknown" without raising.
        entry = _entry(_write_one(tmp_path, elapsed_ms=None))

        assert entry["time"] == 0
        assert entry["timings"]["wait"] == -1


class TestMultipleExchanges:
    def test_one_entry_per_exchange_in_order(self, tmp_path):
        pairs = [_make_pair(), _make_pair(), _make_pair()]
        for i, (request, _) in enumerate(pairs):
            request.headers["x-iteration"] = str(i)

        path = write_har_file(tmp_path, "test_parallel", [(request, response, None) for request, response in pairs])

        entries = json.loads(path.read_text(encoding="utf-8"))["log"]["entries"]
        order = [{h["name"]: h["value"] for h in e["request"]["headers"]}["x-iteration"] for e in entries]
        assert order == ["0", "1", "2"]

    def test_missing_response_written_as_status_zero(self, tmp_path):
        """A timed-out request still produces a HAR entry: the request side is
        real, the response side is the browser-convention status-0 stub."""
        request, _ = _make_pair()

        entry = _entry(write_har_file(tmp_path, "test_timeout", [(request, None, None)]))

        assert entry["request"]["url"] == "https://example.com/api/users"
        assert entry["response"] == {
            "status": 0,
            "statusText": "",
            "httpVersion": "HTTP/1.1",
            "cookies": [],
            "headers": [],
            "content": {"size": 0, "mimeType": "x-unknown"},
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": -1,
        }
        assert entry["comment"] == "No response received (request failed or timed out)"


class TestPerExchangeStartTimes:
    """Each exchange carries its own start timestamp; HAR waterfalls are built
    from startedDateTime, so per-entry truth matters."""

    def test_entries_carry_their_own_start_times(self, tmp_path):
        req1, resp1 = _make_pair()
        req2, resp2 = _make_pair()
        t0 = datetime.datetime(2026, 7, 22, 10, 0, 0, tzinfo=datetime.UTC)
        t1 = datetime.datetime(2026, 7, 22, 10, 0, 5, tzinfo=datetime.UTC)
        path = write_har_file(tmp_path, "t", [(req1, resp1, t0), (req2, resp2, t1)])
        assert [_entry(path, i)["startedDateTime"] for i in (0, 1)] == [t0.isoformat(), t1.isoformat()]

    def test_missing_start_time_falls_back_to_write_time(self, tmp_path):
        before = datetime.datetime.now(datetime.UTC)
        path = _write_one(tmp_path)
        after = datetime.datetime.now(datetime.UTC)
        assert before <= datetime.datetime.fromisoformat(_entry(path)["startedDateTime"]) <= after


class TestSerializationFamilies:
    """Direct coverage of request_response_to_har_entry's body/header branches.

    Each family below hits a distinct serializer that the file-level tests
    (JSON body, happy path) never exercise.
    """

    def test_request_cookie_header_parsed(self):
        req = httpx.Request("GET", "https://x.com/p", headers={"cookie": "s=abc; t=def"})
        entry = request_response_to_har_entry(req, httpx.Response(200, request=req))
        assert entry["request"]["cookies"] == [
            {"name": "s", "value": "abc"},
            {"name": "t", "value": "def"},
        ]

    def test_response_set_cookie_serialized(self):
        req = httpx.Request("GET", "https://x.com/p")
        resp = httpx.Response(200, headers={"set-cookie": "session=xyz; Path=/"}, request=req)
        entry = request_response_to_har_entry(req, resp)
        assert entry["response"]["cookies"] == [
            {
                "name": "session",
                "value": "xyz",
                "secure": False,
                "httpOnly": False,
                "path": "/",
                "domain": "x.com",
            }
        ]

    def test_cookie_without_path_or_domain_omits_them(self):
        cookies = httpx.Cookies()
        cookies.set("bare", "1", domain="", path="")  # Cookies.set always marks HttpOnly
        assert har_writer._format_cookies(cookies) == [{"name": "bare", "value": "1", "secure": False, "httpOnly": True}]

    def test_same_name_response_cookies_keep_distinct_scopes(self):
        req = httpx.Request("GET", "https://x.com/")
        resp = httpx.Response(
            200,
            headers=[
                ("set-cookie", "session=root; Path=/; Secure; HttpOnly"),
                ("set-cookie", "session=admin; Path=/admin"),
            ],
            request=req,
        )

        cookies = request_response_to_har_entry(req, resp)["response"]["cookies"]

        assert [(cookie["name"], cookie["value"], cookie["path"]) for cookie in cookies] == [
            ("session", "root", "/"),
            ("session", "admin", "/admin"),
        ]
        assert cookies[0]["secure"] is True
        assert cookies[0]["httpOnly"] is True

    def test_repeated_response_headers_are_not_comma_folded(self):
        """httpx.Headers.items() folds repeated names with ", ". RFC 6265 forbids
        that for Set-Cookie precisely because cookie attributes contain commas, so
        folding two cookies corrupts both in the HAR export."""
        set_cookies = ["a=1; Path=/; Expires=Wed, 21 Oct 2026 07:28:00 GMT", "b=2; Path=/"]
        req = httpx.Request("GET", "https://example.com/")
        resp = httpx.Response(200, headers=[("set-cookie", value) for value in set_cookies], request=req)

        headers = request_response_to_har_entry(req, resp)["response"]["headers"]

        assert [h["value"] for h in headers if h["name"] == "set-cookie"] == set_cookies

    def test_query_string_extracted(self):
        req = httpx.Request("GET", "https://x.com/p?a=1&b=2&a=3")
        entry = request_response_to_har_entry(req, httpx.Response(200, request=req))
        # Repeated keys are preserved as separate entries, per HAR.
        assert entry["request"]["queryString"] == [
            {"name": "a", "value": "1"},
            {"name": "a", "value": "3"},
            {"name": "b", "value": "2"},
        ]

    def test_repeated_form_values_are_separate_scalar_params(self):
        """HAR params are one name/value record per pair, in body order — a
        repeated name is several records, not one record with an array value."""
        req = httpx.Request("POST", "https://x.com", content=b"tag=one&tag=two&empty=", headers={"content-type": "application/x-www-form-urlencoded"})

        post = request_response_to_har_entry(req, httpx.Response(200, request=req))["request"]["postData"]

        assert post["mimeType"] == "application/x-www-form-urlencoded"
        assert post["params"] == [
            {"name": "tag", "value": "one"},
            {"name": "tag", "value": "two"},
            {"name": "empty", "value": ""},
        ]

    def test_binary_request_body_base64_encoded(self):
        raw = b"\xff\xfe\x00"
        req = httpx.Request("POST", "https://x.com", content=raw, headers={"content-type": "application/octet-stream"})
        entry = request_response_to_har_entry(req, httpx.Response(200, request=req))
        post = entry["request"]["postData"]
        assert post["encoding"] == "base64"
        assert base64.b64decode(post["text"]) == raw

    def test_binary_response_body_base64_encoded(self):
        raw = b"\xff\xfe"
        req = httpx.Request("GET", "https://x.com")
        resp = httpx.Response(200, content=raw, headers={"content-type": "application/octet-stream"}, request=req)
        content = request_response_to_har_entry(req, resp)["response"]["content"]
        assert content["encoding"] == "base64"
        assert content["size"] == len(raw)
        assert base64.b64decode(content["text"]) == raw

    def test_empty_response_body_has_no_text(self):
        req = httpx.Request("GET", "https://x.com")
        resp = httpx.Response(204, request=req)
        content = request_response_to_har_entry(req, resp)["response"]["content"]
        assert content["size"] == 0
        assert "text" not in content

    def test_empty_request_body_has_no_post_data(self):
        req = httpx.Request("GET", "https://x.com")
        entry = request_response_to_har_entry(req, httpx.Response(200, request=req))
        assert "postData" not in entry["request"]


_LONG_PREFIX = "tests/test_mod.http.json::mod::test_stage[" + "x" * 500


class TestFilenames:
    def test_clean_name_is_used_verbatim(self, tmp_path):
        path = _write_one(tmp_path, "plain_name")
        assert path == tmp_path / "plain_name.har"
        assert path.exists()

    def test_windows_illegal_characters_are_replaced(self, tmp_path):
        """Parametrize ids put '?', '*', quotes and friends into nodeids. They
        are legal on Linux but reject the write on Windows, and the plugin only
        logs a warning when the write fails — the HAR would vanish silently."""
        path = _write_one(tmp_path, 'tests/t.py::test_q[why? a*b "x"<y>|z]')

        assert not set(path.name) & set('<>:"/\\|?*')
        assert path.exists()
        assert "test_q" in path.name, "a human must still recognize which test the .har belongs to"

    @pytest.mark.parametrize(
        "nodeid",
        [
            pytest.param("tests/deeply/nested/test_mod.http.json::mod::test_stage[" + "x" * 500 + "]", id="long"),
            # The cap counts bytes, not characters.
            pytest.param("tests/test_mod.http.json::mod::test_stage[" + "д" * 300 + "]", id="multi-byte"),
        ],
    )
    def test_filename_stays_within_the_byte_limit(self, tmp_path, nodeid):
        """255 bytes is the per-component cap on ext4/APFS/NTFS: a deep scenario
        path plus long parametrize ids overruns it and the write raises OSError."""
        path = _write_one(tmp_path, nodeid)

        assert len(path.name.encode()) <= 255
        assert path.exists()

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            # Sanitization maps '/', '\\' and ':' all to '_'.
            pytest.param("t/x", "t:x", id="sanitized"),
            # Two nodeids sharing a long prefix truncate to the same stem.
            pytest.param(_LONG_PREFIX + "-alpha]", _LONG_PREFIX + "-beta]", id="truncated"),
        ],
    )
    def test_lossy_names_do_not_collide(self, tmp_path, first, second):
        """Both sanitization and truncation are lossy, so distinct nodeids must
        still get distinct .har paths rather than overwrite each other."""
        p1 = _write_one(tmp_path, first)
        p2 = _write_one(tmp_path, second)

        assert p1 != p2
        assert p1.exists()
        assert p2.exists()
