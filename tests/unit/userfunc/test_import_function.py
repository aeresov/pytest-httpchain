"""Tests for import_function."""

import base64
import json
import os.path
import urllib.parse
import wsgiref.simple_server

import pytest

from pytest_httpchain.userfunc import UserFunctionError, import_function


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("json:loads", json.loads, id="top-level-module"),
        pytest.param("os.path:join", os.path.join, id="dotted-module"),
        pytest.param("urllib.parse:urlencode", urllib.parse.urlencode, id="package-submodule"),
        pytest.param("wsgiref.simple_server:demo_app", wsgiref.simple_server.demo_app, id="underscore-in-module"),
        pytest.param("base64:b64encode", base64.b64encode, id="digits-in-module"),
    ],
)
def test_returns_the_named_function(name, expected):
    assert import_function(name) is expected


def test_missing_module():
    """H6: the ImportError text is in the message, so a missing module reads
    differently from one that fails to import."""
    with pytest.raises(UserFunctionError, match="Failed to import module 'nonexistent_module_xyz': No module named"):
        import_function("nonexistent_module_xyz:some_func")


def test_module_top_level_error_is_wrapped(tmp_path, monkeypatch):
    """M33: a module whose top-level code raises a non-ImportError is wrapped
    as UserFunctionError instead of escaping as a raw traceback."""
    (tmp_path / "boom_module.py").write_text("raise RuntimeError('top-level boom')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(UserFunctionError, match="Failed to import module 'boom_module': top-level boom") as exc_info:
        import_function("boom_module:whatever")
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_function_not_in_module():
    with pytest.raises(UserFunctionError, match="Function 'nonexistent_function_xyz' not found in module 'json'"):
        import_function("json:nonexistent_function_xyz")


def test_non_callable_attribute():
    with pytest.raises(UserFunctionError, match="'os:name' is not callable"):
        import_function("os:name")
