"""Tests for wrap_function functionality."""

import pytest
from pytest_httpchain_userfunc import UserFunctionError, wrap_function


# Module-level functions for scope testing (import_function only checks f_globals)
def concat_three(a, b, c):
    """Concatenate three values with dashes."""
    return f"{a}-{b}-{c}"


def concat_four(a, b, c, d):
    """Concatenate four values with dots."""
    return f"{a}.{b}.{c}.{d}"


def format_message(prefix, message, suffix="!"):
    """Format a message with prefix and suffix."""
    return f"{prefix}: {message}{suffix}"


def full_function(a, b, c, x=1, y=2, z=3):
    """Function with positional and keyword args."""
    return f"{a}{b}{c}-{x}{y}{z}"


def always_fails():
    """Function that always raises RuntimeError."""
    raise RuntimeError("always fails")


def raises_user_error():
    """Function that raises UserFunctionError."""
    raise UserFunctionError("custom error")


def needs_three_args(a, b, c):
    """Function requiring exactly three args."""
    return a + b + c


class TestWrapFunctionBasic:
    """Basic wrap_function tests."""

    def test_wrapped_basic_call(self):
        """Create a wrapped callable and call it."""
        wrapped = wrap_function("json:loads")
        result = wrapped('{"key": "value"}')
        assert result == {"key": "value"}

    def test_wrapped_is_callable(self):
        """Wrapped result is callable."""
        wrapped = wrap_function("json:dumps")
        assert callable(wrapped)

    def test_wrapped_multiple_calls(self):
        """Wrapped function can be called multiple times."""
        wrapped = wrap_function("conftest_helper")
        assert wrapped(1, 2) == 3
        assert wrapped(10, 20) == 30
        assert wrapped(0, 0) == 0


class TestWrapFunctionDefaultArgs:
    """Tests for default_args behavior."""

    def test_default_args_prepended(self):
        """Default args are prepended to call-time args."""
        wrapped = wrap_function("concat_three", default_args=["first"])
        result = wrapped("second", "third")
        assert result == "first-second-third"

    def test_default_args_multiple(self):
        """Multiple default args are prepended in order."""
        wrapped = wrap_function("concat_four", default_args=["one", "two"])
        result = wrapped("three", "four")
        assert result == "one.two.three.four"

    def test_default_args_empty_list(self):
        """Empty default_args works like no default args."""
        wrapped = wrap_function("conftest_helper", default_args=[])
        result = wrapped(5, 5)
        assert result == 10

    def test_default_args_none(self):
        """None default_args works like no default args."""
        wrapped = wrap_function("conftest_helper", default_args=None)
        result = wrapped(3, 4)
        assert result == 7


class TestWrapFunctionDefaultKwargs:
    """Tests for default_kwargs behavior."""

    def test_default_kwargs_applied(self):
        """Default kwargs are applied to the call."""
        wrapped = wrap_function("json:dumps", default_kwargs={"indent": 2})
        result = wrapped({"a": 1})
        assert "\n" in result  # indented output has newlines

    def test_call_kwargs_override_default(self):
        """Call-time kwargs override default kwargs."""
        wrapped = wrap_function("conftest_with_kwargs", default_kwargs={"name": "default"})
        # Call with override
        result = wrapped(name="override")
        assert result == "hello, override"

    def test_default_kwargs_empty_dict(self):
        """Empty default_kwargs works like no default kwargs."""
        wrapped = wrap_function("json:dumps", default_kwargs={})
        result = wrapped({"x": 1})
        assert result == '{"x": 1}'

    def test_default_kwargs_none(self):
        """None default_kwargs works like no default kwargs."""
        wrapped = wrap_function("json:dumps", default_kwargs=None)
        result = wrapped({"y": 2})
        assert result == '{"y": 2}'

    def test_default_kwargs_merged_with_call(self):
        """Default kwargs and call kwargs are merged."""
        wrapped = wrap_function("json:dumps", default_kwargs={"indent": 2})
        result = wrapped({"a": 1}, sort_keys=True)
        # Both indent (from default) and sort_keys (from call) should apply
        assert "\n" in result


class TestWrapFunctionCombined:
    """Tests for combined default_args and default_kwargs."""

    def test_both_defaults_applied(self):
        """Both default_args and default_kwargs work together."""
        wrapped = wrap_function(
            "format_message",
            default_args=["INFO"],
            default_kwargs={"suffix": "..."},
        )
        result = wrapped("Processing")
        assert result == "INFO: Processing..."

    def test_call_args_and_kwargs_merge_with_defaults(self):
        """Call-time args/kwargs merge correctly with defaults."""
        wrapped = wrap_function(
            "full_function",
            default_args=["A"],
            default_kwargs={"x": 10, "y": 20},
        )
        result = wrapped("B", "C", z=30)
        assert result == "ABC-102030"


class TestWrapFunctionName:
    """Tests for wrapped function __name__ attribute."""

    def test_wrapped_name_simple(self):
        """Wrapped function has descriptive __name__."""
        wrapped = wrap_function("conftest_helper")
        assert "conftest_helper" in wrapped.__name__  # type: ignore[attr-defined]
        assert wrapped.__name__.startswith("wrapped_")  # type: ignore[attr-defined]

    def test_wrapped_name_with_module(self):
        """Wrapped function name includes module info."""
        wrapped = wrap_function("json:loads")
        assert "json" in wrapped.__name__  # type: ignore[attr-defined]
        assert "loads" in wrapped.__name__  # type: ignore[attr-defined]

    def test_wrapped_name_nested_module(self):
        """Wrapped function name handles nested modules."""
        wrapped = wrap_function("os.path:join")
        assert "os" in wrapped.__name__  # type: ignore[attr-defined]
        assert "path" in wrapped.__name__  # type: ignore[attr-defined]
        assert "join" in wrapped.__name__  # type: ignore[attr-defined]


class TestWrapFunctionErrors:
    """Error handling tests for wrap_function."""

    def test_import_error_on_call(self):
        """Import errors occur at call time, not wrap time."""
        # Wrapping doesn't fail
        wrapped = wrap_function("nonexistent_module:func")
        assert callable(wrapped)

        # Calling fails
        with pytest.raises(UserFunctionError, match="Failed to import module"):
            wrapped()

    def test_not_found_error_on_call(self):
        """Not found errors occur at call time."""
        wrapped = wrap_function("nonexistent_function_xyz")

        with pytest.raises(UserFunctionError, match="not found"):
            wrapped()

    def test_runtime_error_wrapped(self):
        """Runtime errors during execution are wrapped in UserFunctionError."""
        wrapped = wrap_function("always_fails")

        with pytest.raises(UserFunctionError, match="Error calling function") as exc_info:
            wrapped()

        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, RuntimeError)

    def test_user_function_error_preserved(self):
        """UserFunctionError from import is re-raised as-is."""
        wrapped = wrap_function("raises_user_error")

        with pytest.raises(UserFunctionError, match="custom error"):
            wrapped()

    def test_type_error_from_bad_args(self):
        """TypeError from wrong arguments is wrapped."""
        wrapped = wrap_function("needs_three_args", default_args=["only_one"])

        with pytest.raises(UserFunctionError, match="Error calling function"):
            wrapped()  # Missing two more args
