"""Tests for call_function functionality."""

import pytest
from pytest_httpchain_userfunc import UserFunctionError, call_function


class TestCallFunctionBasic:
    """Basic call_function tests."""

    def test_call_with_positional_args(self):
        result = call_function("json:dumps", {"a": 1})
        assert result == '{"a": 1}'

    def test_call_with_kwargs(self):
        result = call_function("json:dumps", {"b": 2}, indent=2)
        assert '"b": 2' in result
        assert "\n" in result

    def test_call_with_mixed_args(self):
        result = call_function("json:dumps", {"c": 3}, sort_keys=True, indent=None)
        assert result == '{"c": 3}'

    def test_call_no_args(self):
        result = call_function("test_helpers:helper_no_args")
        assert result == "helper_result"

    def test_call_helper_function(self):
        result = call_function("test_helpers:helper_add", 10, 20)
        assert result == 30

    def test_call_helper_with_kwargs(self):
        result = call_function("test_helpers:helper_with_kwargs", name="pytest")
        assert result == "hello, pytest"


class TestCallFunctionErrors:
    """Error handling tests for call_function."""

    def test_propagates_import_error(self):
        with pytest.raises(UserFunctionError, match="Failed to import module"):
            call_function("nonexistent_module:func")

    def test_bare_name_raises(self):
        with pytest.raises(UserFunctionError, match="Module path is required"):
            call_function("nonexistent_function_xyz")

    def test_wraps_runtime_error(self):
        with pytest.raises(UserFunctionError, match="Error calling function") as exc_info:
            call_function("test_helpers:failing_function")

        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, ValueError)
        assert "intentional failure" in str(exc_info.value.__cause__)

    def test_wraps_type_error_wrong_args(self):
        with pytest.raises(UserFunctionError, match="Error calling function"):
            call_function("test_helpers:needs_two_args", 1)

    def test_preserves_exception_chain(self):
        with pytest.raises(UserFunctionError) as exc_info:
            call_function("test_helpers:raises_key_error")

        assert isinstance(exc_info.value.__cause__, KeyError)
