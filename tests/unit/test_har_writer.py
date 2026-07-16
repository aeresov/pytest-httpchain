import datetime
import json

import httpx

from pytest_httpchain.har_writer import write_har_file


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


class TestWriteHarFile:
    def test_creates_file(self, tmp_path):
        request, response = _make_pair()
        path = write_har_file(tmp_path, "test_users", [(request, response)])

        assert path.exists()
        assert path.suffix == ".har"
        assert path.parent == tmp_path

    def test_top_level_shape(self, tmp_path):
        request, response = _make_pair()
        path = write_har_file(tmp_path, "test_users", [(request, response)])

        har = json.loads(path.read_text(encoding="utf-8"))

        assert "log" in har
        log = har["log"]
        assert log["version"] == "1.2"
        assert "creator" in log
        assert log["creator"]["name"] == "pytest-httpchain"
        assert "version" in log["creator"]
        assert isinstance(log["entries"], list)
        assert len(log["entries"]) == 1

    def test_entry_request_and_response_fields(self, tmp_path):
        request, response = _make_pair()
        path = write_har_file(tmp_path, "test_users", [(request, response)])

        entry = json.loads(path.read_text(encoding="utf-8"))["log"]["entries"][0]

        assert entry["request"]["method"] == "POST"
        assert entry["request"]["url"] == "https://example.com/api/users"
        assert entry["response"]["status"] == 201

    def test_timing_is_nonzero(self, tmp_path):
        # M19 regression guard: a real duration derived from response.elapsed
        # must appear instead of 0.
        request, response = _make_pair(elapsed_ms=123.5)
        path = write_har_file(tmp_path, "test_users", [(request, response)])

        entry = json.loads(path.read_text(encoding="utf-8"))["log"]["entries"][0]

        assert isinstance(entry["time"], (int, float))
        assert entry["time"] == 123.5
        assert entry["timings"]["wait"] == 123.5

    def test_elapsed_ms_override(self, tmp_path):
        # An explicit elapsed_ms takes precedence over response.elapsed.
        request, response = _make_pair(elapsed_ms=999.0)
        path = write_har_file(tmp_path, "test_users", [(request, response)], elapsed_ms=42.0)

        entry = json.loads(path.read_text(encoding="utf-8"))["log"]["entries"][0]

        assert entry["time"] == 42.0

    def test_missing_elapsed_is_handled(self, tmp_path):
        # When response.elapsed is unavailable (unread response), timing falls
        # back to 0 without raising.
        request, response = _make_pair(elapsed_ms=None)
        path = write_har_file(tmp_path, "test_users", [(request, response)])

        entry = json.loads(path.read_text(encoding="utf-8"))["log"]["entries"][0]

        assert isinstance(entry["time"], (int, float))
        assert entry["time"] == 0

    def test_unsafe_test_name_is_sanitized(self, tmp_path):
        request, response = _make_pair()
        path = write_har_file(tmp_path, "tests/foo.py::test_bar", [(request, response)])

        assert path.exists()
        assert "/" not in path.name
        assert ":" not in path.name


class TestMultipleExchanges:
    def test_one_entry_per_exchange_in_order(self, tmp_path):
        pairs = [_make_pair(), _make_pair(), _make_pair()]
        for i, (request, _) in enumerate(pairs):
            request.headers["x-iteration"] = str(i)

        path = write_har_file(tmp_path, "test_parallel", pairs)

        entries = json.loads(path.read_text(encoding="utf-8"))["log"]["entries"]
        assert len(entries) == 3
        order = [{h["name"]: h["value"] for h in e["request"]["headers"]}["x-iteration"] for e in entries]
        assert order == ["0", "1", "2"]

    def test_missing_response_written_as_status_zero(self, tmp_path):
        """A timed-out request still produces a HAR entry: the request side is
        real, the response side is the browser-convention status-0 stub."""
        request, _ = _make_pair()

        path = write_har_file(tmp_path, "test_timeout", [(request, None)])

        entry = json.loads(path.read_text(encoding="utf-8"))["log"]["entries"][0]
        assert entry["request"]["url"] == "https://example.com/api/users"
        assert entry["response"]["status"] == 0
        assert "No response received" in entry["comment"]
