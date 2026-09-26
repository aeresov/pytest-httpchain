"""Tests for wrap_function. Error wrapping itself is call_function's (see
test_call_function.py); a wrapped function only delegates to it."""

import pytest

from pytest_httpchain.userfunc import UserFunctionError, wrap_function

ECHO = "userfunc_test_helpers:echo"


def test_wrapped_forwards_the_call():
    assert wrap_function(ECHO)(1, k=2) == ((1,), {"k": 2})


def test_call_kwargs_win_over_default_kwargs():
    """Merged per call: a call-time kwarg wins on conflict, and does not leak
    into the defaults of later calls."""
    wrapped = wrap_function(ECHO, default_kwargs={"a": 1, "b": "default"})
    assert wrapped(b="call", c=3) == ((), {"a": 1, "b": "call", "c": 3})
    assert wrapped() == ((), {"a": 1, "b": "default"})


@pytest.mark.parametrize("default_kwargs", [{}, None])
def test_no_default_kwargs(default_kwargs):
    assert wrap_function(ECHO, default_kwargs=default_kwargs)(a=1) == ((), {"a": 1})


@pytest.mark.parametrize(("name", "expected"), [("json:loads", "wrapped_json_loads"), ("os.path:join", "wrapped_os_path_join")])
def test_wrapped_name(name, expected):
    assert wrap_function(name).__name__ == expected


def test_import_deferred_until_call():
    """Wrapping never imports, so a bad name surfaces only when called."""
    wrapped = wrap_function("nonexistent_module:func")
    with pytest.raises(UserFunctionError, match="Failed to import module"):
        wrapped()
