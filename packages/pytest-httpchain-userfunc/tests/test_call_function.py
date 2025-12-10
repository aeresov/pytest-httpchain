"""Tests for call_function functionality."""

import pytest
from pytest_httpchain_userfunc import UserFunctionError, call_function


# Module-level functions for scope testing (import_function only checks f_globals)
def multiply_numbers(x, y):
    """Multiply two numbers."""
    return x * y


def divide_numbers(a, b):
    """Divide a by b."""
    return a / b


def failing_function():
    """Function that always raises ValueError."""
    raise ValueError("intentional failure")


def needs_two_args(a, b):
    """Function requiring exactly two args."""
    return a + b


def raises_key_error():
    """Function that raises KeyError."""
    d = {}
    return d["missing"]


class TestCallFunctionBasic:
    """Basic call_function tests."""

    def test_call_with_positional_args(self):
        """Call a function with positional arguments."""
        result = call_function("json:dumps", {"a": 1})
        assert result == '{"a": 1}'

    def test_call_with_kwargs(self):
        """Call a function with keyword arguments."""
        result = call_function("json:dumps", {"b": 2}, indent=2)
        assert '"b": 2' in result
        assert "\n" in result  # indented output has newlines

    def test_call_with_mixed_args(self):
        """Call a function with both positional and keyword arguments."""
        result = call_function("json:dumps", {"c": 3}, sort_keys=True, indent=None)
        assert result == '{"c": 3}'

    def test_call_no_args(self):
        """Call a function that takes no arguments."""
        result = call_function("conftest_no_args")
        assert result == "conftest_result"

    def test_call_conftest_function(self):
        """Call a function defined in conftest."""
        result = call_function("conftest_helper", 10, 20)
        assert result == 30

    def test_call_conftest_with_kwargs(self):
        """Call a conftest function with keyword arguments."""
        result = call_function("conftest_with_kwargs", name="pytest")
        assert result == "hello, pytest"


class TestCallFunctionErrors:
    """Error handling tests for call_function."""

    def test_propagates_import_error(self):
        """Import errors are propagated as UserFunctionError."""
        with pytest.raises(UserFunctionError, match="Failed to import module"):
            call_function("nonexistent_module:func")

    def test_propagates_not_found_error(self):
        """Not found errors are propagated as UserFunctionError."""
        with pytest.raises(UserFunctionError, match="not found"):
            call_function("nonexistent_function_xyz")

    def test_wraps_runtime_error(self):
        """Runtime errors during function execution are wrapped."""
        with pytest.raises(UserFunctionError, match="Error calling function") as exc_info:
            call_function("failing_function")

        # Verify exception chaining
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, ValueError)
        assert "intentional failure" in str(exc_info.value.__cause__)

    def test_wraps_type_error_wrong_args(self):
        """TypeError from wrong arguments is wrapped."""
        with pytest.raises(UserFunctionError, match="Error calling function"):
            call_function("needs_two_args", 1)  # Missing second arg

    def test_preserves_exception_chain(self):
        """The original exception is preserved in __cause__."""
        with pytest.raises(UserFunctionError) as exc_info:
            call_function("raises_key_error")

        assert isinstance(exc_info.value.__cause__, KeyError)


class TestCallFunctionWithModuleScope:
    """Tests for calling functions from module scope."""

    def test_call_module_level_function(self):
        """Call a function defined at module level."""
        result = call_function("multiply_numbers", 6, 7)
        assert result == 42

    def test_call_from_nested_context(self):
        """Call a module-level function from a nested call context."""

        def do_call():
            return call_function("divide_numbers", 100, 4)

        result = do_call()
        assert result == 25.0
