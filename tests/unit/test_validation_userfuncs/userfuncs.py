"""Importable helper functions for deep-validation tests.

This directory is added to sys.path via the validator's ``--syspath`` option so
that ``userfuncs:<name>`` references resolve during deep validation tests.
"""


def good_auth():
    """An auth function callable with no arguments."""
    return None


def auth_with_required(token):
    """An auth function that requires an argument (auth is called with none)."""
    return token


def auth_posonly(token, /):
    """An auth function with a required positional-only parameter.

    The framework only ever passes arguments by keyword (and none at all for
    auth), so this can never be satisfied at runtime."""
    return token


def needs_response(response):
    """A save/verify function that accepts only the injected response."""
    return True


def needs_response_and_x(response, x):
    """A save/verify function that needs an extra required argument."""
    return bool(x)


def accepts_kwargs(response, **kwargs):
    """A function that accepts arbitrary keyword arguments."""
    return True
