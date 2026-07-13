"""Helper functions for userfunc tests, imported via '_helpers:func_name'."""

from pytest_httpchain.userfunc import UserFunctionError


def helper_add(x: int, y: int) -> int:
    return x + y


def helper_no_args() -> str:
    return "helper_result"


def helper_with_kwargs(*, name: str = "default") -> str:
    return f"hello, {name}"


def multiply_numbers(x, y):
    return x * y


def divide_numbers(a, b):
    return a / b


def failing_function():
    raise ValueError("intentional failure")


def needs_two_args(a, b):
    return a + b


def raises_key_error():
    d = {}
    return d["missing"]


def always_fails():
    raise RuntimeError("always fails")


def raises_user_error():
    raise UserFunctionError("custom error")


def needs_three_args(a, b, c):
    return a + b + c


not_callable = "I am a string, not a function"
