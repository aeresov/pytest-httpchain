"""Importable verify functions for the ``response_steps`` tests.

A plain module for the reason ``utils_test_helpers`` is one: a user function
is named by a dotted import string, and one naming the test module would load
a second copy of it under ``--import-mode=importlib``.
"""

import pytest

# Where `records` notes that it was called (a test sets its own list).
EVENTS: list[object] = []


def returns_true(response):
    return True


def returns_false(response):
    return False


def returns_none(response):
    return None


def raises(response):
    raise ValueError("boom")


def skips(response):
    pytest.skip("not on this server")


def xfails(response):
    pytest.xfail("known bug")


def fails(response):
    pytest.fail("custom check failed")


def records(response):
    EVENTS.append("called")
    return True
