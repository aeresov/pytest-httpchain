"""Helper functions for userfunc tests, imported via 'userfunc_test_helpers:func_name'."""

from pytest_httpchain.userfunc import UserFunctionError


def echo(*args, **kwargs):
    """Return exactly what it was called with."""
    return args, kwargs


def failing_function():
    raise ValueError("intentional failure")


def needs_two_args(a, b):
    return a + b


def raises_user_error():
    raise UserFunctionError("custom error")
