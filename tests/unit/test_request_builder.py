"""request_builder: resolved Request models -> httpx kwargs.

The success path of every body type runs end to end in
tests/integration/test_body_types.py; this pins the mapping details and the
error paths a server round trip cannot reach.
"""

import pytest

from pytest_httpchain.errors import RequestError
from pytest_httpchain.models import BinaryBody, FilesBody, Request
from pytest_httpchain.request_builder import build_request_kwargs


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param(lambda d: BinaryBody(binary="/nonexistent/file.bin"), "Binary file not found", id="binary-missing"),
        pytest.param(lambda d: FilesBody(files={"upload": "/nonexistent/file.txt"}), "File not found for upload", id="files-missing"),
        # A directory raises IsADirectoryError: an OSError that is NOT a
        # FileNotFoundError, so only a broadened handler turns it into a
        # stage failure (M2).
        pytest.param(lambda d: BinaryBody(binary=str(d)), "Cannot read binary file", id="binary-unreadable"),
        pytest.param(lambda d: FilesBody(files={"upload": str(d)}), "Cannot read file for upload", id="files-unreadable"),
    ],
)
def test_unreadable_body_file_is_a_request_error(tmp_path, body, message):
    with pytest.raises(RequestError, match=message):
        build_request_kwargs(Request(url="https://example.com/api", method="POST", body=body(tmp_path)))


def test_empty_params_do_not_override_url_query():
    """httpx replaces the URL's own query with an explicit params={}."""
    assert build_request_kwargs(Request(url="https://example.com/api?streamId=123"))["params"] is None


def test_params_passed_through():
    assert build_request_kwargs(Request(url="https://example.com/api", params={"key": "value"}))["params"] == {"key": "value"}


@pytest.mark.parametrize(("declared", "follow"), [({}, True), ({"allow_redirects": False}, False)], ids=["default", "disabled"])
def test_allow_redirects_maps_to_follow_redirects(declared, follow):
    """httpx defaults follow_redirects to False, so silently dropping the
    mapping would flip the plugin's documented follow-by-default behavior."""
    assert build_request_kwargs(Request.model_validate({"url": "http://t/", **declared}))["follow_redirects"] is follow
