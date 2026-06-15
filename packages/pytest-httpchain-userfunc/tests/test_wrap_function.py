"""Tests for wrap_function functionality."""

import pytest
from pytest_httpchain_userfunc import UserFunctionError, wrap_function


class TestWrapFunctionBasic:
    """Basic wrap_function tests."""

    def test_wrapped_basic_call(self):
        wrapped = wrap_function("json:loads")
        result = wrapped('{"key": "value"}')
        assert result == {"key": "value"}

    def test_wrapped_is_callable(self):
        wrapped = wrap_function("json:dumps")
        assert callable(wrapped)

    def test_wrapped_multiple_calls(self):
        wrapped = wrap_function("_helpers:helper_add")
        assert wrapped(1, 2) == 3
        assert wrapped(10, 20) == 30
        assert wrapped(0, 0) == 0


class TestWrapFunctionDefaultKwargs:
    """Tests for default_kwargs behavior."""

    def test_default_kwargs_applied(self):
        wrapped = wrap_function("json:dumps", default_kwargs={"indent": 2})
        result = wrapped({"a": 1})
        assert "\n" in result

    def test_call_kwargs_override_default(self):
        wrapped = wrap_function("_helpers:helper_with_kwargs", default_kwargs={"name": "default"})
        result = wrapped(name="override")
        assert result == "hello, override"

    def test_default_kwargs_empty_dict(self):
        wrapped = wrap_function("json:dumps", default_kwargs={})
        result = wrapped({"x": 1})
        assert result == '{"x": 1}'

    def test_default_kwargs_none(self):
        wrapped = wrap_function("json:dumps", default_kwargs=None)
        result = wrapped({"y": 2})
        assert result == '{"y": 2}'

    def test_default_kwargs_merged_with_call(self):
        wrapped = wrap_function("json:dumps", default_kwargs={"indent": 2})
        result = wrapped({"a": 1}, sort_keys=True)
        assert "\n" in result


class TestWrapFunctionName:
    """Tests for wrapped function __name__ attribute."""

    def test_wrapped_name_with_module(self):
        wrapped = wrap_function("json:loads")
        assert "json" in wrapped.__name__
        assert "loads" in wrapped.__name__
        assert wrapped.__name__.startswith("wrapped_")

    def test_wrapped_name_nested_module(self):
        wrapped = wrap_function("os.path:join")
        assert "os" in wrapped.__name__
        assert "path" in wrapped.__name__
        assert "join" in wrapped.__name__


class TestWrapFunctionErrors:
    """Error handling tests for wrap_function."""

    def test_import_error_on_call(self):
        wrapped = wrap_function("nonexistent_module:func")
        assert callable(wrapped)

        with pytest.raises(UserFunctionError, match="Failed to import module"):
            wrapped()

    def test_bare_name_error_on_call(self):
        wrapped = wrap_function("nonexistent_function_xyz")

        with pytest.raises(UserFunctionError, match="Module path is required"):
            wrapped()

    def test_runtime_error_wrapped(self):
        wrapped = wrap_function("_helpers:always_fails")

        with pytest.raises(UserFunctionError, match="Error calling function") as exc_info:
            wrapped()

        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, RuntimeError)

    def test_user_function_error_preserved(self):
        wrapped = wrap_function("_helpers:raises_user_error")

        with pytest.raises(UserFunctionError, match="custom error"):
            wrapped()

    def test_type_error_from_bad_args(self):
        wrapped = wrap_function("_helpers:needs_three_args")

        with pytest.raises(UserFunctionError, match="Error calling function"):
            wrapped("only_one")
