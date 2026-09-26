"""Tests for call_user_function: the dispatch over the UserFunctionCall model union."""

import pytest

from pytest_httpchain.models import UserFunctionKwargs, UserFunctionName
from pytest_httpchain.userfunc import call_user_function

ECHO = UserFunctionName("userfunc_test_helpers:echo")


@pytest.mark.parametrize(
    ("func_call", "extra_kwargs", "expected"),
    [
        pytest.param(ECHO, {}, {}, id="name"),
        pytest.param(ECHO, {"a": 10, "b": 20}, {"a": 10, "b": 20}, id="name-with-extra"),
        pytest.param(UserFunctionKwargs(name=ECHO, kwargs={"a": 1, "b": 2}), {}, {"a": 1, "b": 2}, id="declared"),
        pytest.param(UserFunctionKwargs(name=ECHO, kwargs={"a": 1, "b": 2}), {"c": "extra"}, {"a": 1, "b": 2, "c": "extra"}, id="declared-with-extra"),
        pytest.param(UserFunctionKwargs(name=ECHO, kwargs={"a": 1, "c": "declared"}), {"c": "extra"}, {"a": 1, "c": "extra"}, id="extra-wins"),
    ],
)
def test_extra_kwargs_merge_over_declared_kwargs(func_call, extra_kwargs, expected):
    """``extra_kwargs`` carries the ``response`` for verify/save functions, so
    it wins over a same-named declared kwarg."""
    assert call_user_function(func_call, **extra_kwargs) == ((), expected)


@pytest.mark.parametrize("func_call", ["invalid_string", None], ids=["str", "NoneType"])
def test_unhandled_call_type_is_a_plugin_bug(func_call):
    """Unreachable from a validated scenario, so it surfaces as a RuntimeError,
    not a stage failure."""
    with pytest.raises(RuntimeError, match=f"Unhandled function call: {type(func_call).__name__}"):
        call_user_function(func_call)
