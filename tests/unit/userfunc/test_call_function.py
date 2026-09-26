"""Tests for call_function."""

import pytest

from pytest_httpchain.userfunc import UserFunctionError, call_function


def test_forwards_args_and_kwargs():
    assert call_function("userfunc_test_helpers:echo", 1, 2, k=3) == ((1, 2), {"k": 3})


@pytest.mark.parametrize(
    ("helper", "args", "cause", "message"),
    [
        ("failing_function", (), ValueError, "intentional failure"),
        ("needs_two_args", (1,), TypeError, "missing 1 required positional argument"),
    ],
)
def test_wraps_exception_with_cause_in_message(helper, args, cause, message):
    """H6: consumers render only str(e) (pytrace=False), never __cause__, so
    the cause's text must be in the message as well as chained."""
    name = f"userfunc_test_helpers:{helper}"
    with pytest.raises(UserFunctionError, match=f"Error calling function '{name}': .*{message}") as exc_info:
        call_function(name, *args)
    assert isinstance(exc_info.value.__cause__, cause)


def test_user_function_error_propagates_unwrapped():
    """A UserFunctionError from the user function is already curated: it must
    not be double-wrapped in another "Error calling function"."""
    with pytest.raises(UserFunctionError, match="^custom error$"):
        call_function("userfunc_test_helpers:raises_user_error")
