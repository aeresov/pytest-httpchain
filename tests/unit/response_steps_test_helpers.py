"""Importable verify functions for the ``response_steps`` tests.

A plain module for the reason ``utils_test_helpers`` is one: a user function
is named by a dotted import string, and one naming the test module would load
a second copy of it under ``--import-mode=importlib``.
"""

import pytest

from pytest_httpchain.errors import SaveError, VerificationError

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


def not_done_yet(response):
    raise VerificationError("job still pending")


def not_done_ever(response):
    raise VerificationError("job failed for good", retryable=False)


def not_saved_yet(response):
    raise SaveError("no result yet")


def done_or_not_yet(response):
    """Wait for a done job, saying "not yet" as a failed check."""
    if response.json()["status"] != "done":
        raise VerificationError("job still pending")
    return True


def result_or_not_yet(response):
    """Save a done job's number, saying "not yet" as a failed extraction."""
    if response.json()["status"] != "done":
        raise SaveError("no result yet")
    return {"n": response.json()["n"]}


def fails_once_done(response):
    """False while the job is pending, then pytest.fail()."""
    if response.json()["status"] != "done":
        return False
    pytest.fail("job vanished")


def skips_once_done(response):
    if response.json()["status"] != "done":
        return False
    pytest.skip("job moved elsewhere")


def xfails_once_done(response):
    if response.json()["status"] != "done":
        return False
    pytest.xfail("known bug")


def skips(response):
    pytest.skip("not on this server")


def xfails(response):
    pytest.xfail("known bug")


def fails(response):
    pytest.fail("custom check failed")


def records(response):
    EVENTS.append("called")
    return True


def saves_item_id(response):
    """Save the item's id under a name only this function knows: no save
    step declares it, so `validate` cannot see it."""
    return {"item_id": response.json()["id"]}
