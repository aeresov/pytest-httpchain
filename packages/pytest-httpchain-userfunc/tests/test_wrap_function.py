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
        wrapped = wrap_function("test_helpers:helper_add")
        assert wrapped(1, 2) == 3
        assert wrapped(10, 20) == 30
        assert wrapped(0, 0) == 0


class TestWrapFunctionDefaultArgs:
    """Tests for default_args behavior."""

    def test_default_args_prepended(self):
        wrapped = wrap_function("test_helpers:concat_three", default_args=["first"])
        result = wrapped("second", "third")
        assert result == "first-second-third"

    def test_default_args_multiple(self):
        wrapped = wrap_function("test_helpers:concat_four", default_args=["one", "two"])
        result = wrapped("three", "four")
        assert result == "one.two.three.four"

    def test_default_args_empty_list(self):
        wrapped = wrap_function("test_helpers:helper_add", default_args=[])
        result = wrapped(5, 5)
        assert result == 10

    def test_default_args_none(self):
        wrapped = wrap_function("test_helpers:helper_add", default_args=None)
        result = wrapped(3, 4)
        assert result == 7


class TestWrapFunctionDefaultKwargs:
    """Tests for default_kwargs behavior."""

    def test_default_kwargs_applied(self):
        wrapped = wrap_function("json:dumps", default_kwargs={"indent": 2})
        result = wrapped({"a": 1})
        assert "\n" in result

    def test_call_kwargs_override_default(self):
        wrapped = wrap_function("test_helpers:helper_with_kwargs", default_kwargs={"name": "default"})
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


class TestWrapFunctionCombined:
    """Tests for combined default_args and default_kwargs."""

    def test_both_defaults_applied(self):
        wrapped = wrap_function(
            "test_helpers:format_message",
            default_args=["INFO"],
            default_kwargs={"suffix": "..."},
        )
        result = wrapped("Processing")
        assert result == "INFO: Processing..."

    def test_call_args_and_kwargs_merge_with_defaults(self):
        wrapped = wrap_function(
            "test_helpers:full_function",
            default_args=["A"],
            default_kwargs={"x": 10, "y": 20},
        )
        result = wrapped("B", "C", z=30)
        assert result == "ABC-102030"


class TestWrapFunctionName:
    """Tests for wrapped function __name__ attribute."""

    def test_wrapped_name_with_module(self):
        wrapped = wrap_function("json:loads")
        assert "json" in wrapped.__name__  # type: ignore[attr-defined]
        assert "loads" in wrapped.__name__  # type: ignore[attr-defined]
        assert wrapped.__name__.startswith("wrapped_")  # type: ignore[attr-defined]

    def test_wrapped_name_nested_module(self):
        wrapped = wrap_function("os.path:join")
        assert "os" in wrapped.__name__  # type: ignore[attr-defined]
        assert "path" in wrapped.__name__  # type: ignore[attr-defined]
        assert "join" in wrapped.__name__  # type: ignore[attr-defined]


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
        wrapped = wrap_function("test_helpers:always_fails")

        with pytest.raises(UserFunctionError, match="Error calling function") as exc_info:
            wrapped()

        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, RuntimeError)

    def test_user_function_error_preserved(self):
        wrapped = wrap_function("test_helpers:raises_user_error")

        with pytest.raises(UserFunctionError, match="custom error"):
            wrapped()

    def test_type_error_from_bad_args(self):
        wrapped = wrap_function("test_helpers:needs_three_args", default_args=["only_one"])

        with pytest.raises(UserFunctionError, match="Error calling function"):
            wrapped()
