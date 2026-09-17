"""Shared factories for the model unit tests.

Most of these tests only need a *valid, incidental* Stage or Request as
scaffolding while they exercise one specific field. These helpers provide that
minimal object so a call site can pass just the field under test instead of
re-spelling the whole ``Stage(name=..., request=Request(url=...), ...)``
boilerplate. A plain helpers module (not conftest.py, which pytest treats as
a plugin file, not an import target)::

    from tests.unit.models.helpers import make_stage, make_request
"""

from typing import Any

import pytest
from pydantic import ValidationError

from pytest_httpchain.models import Request, Stage


def assert_error_types(exc_info: pytest.ExceptionInfo[ValidationError], *expected_types: str, at: str | None = None) -> None:
    """Assert the raised ValidationError carries each given pydantic error ``type``.

    Pydantic's built-in error *codes* (``extra_forbidden``, ``union_tag_invalid``,
    ``too_short``, ``too_long``, ...) are stable across versions, whereas the
    human-readable messages ("Extra inputs are not permitted", ...) are not — so
    tests for built-in validation failures should assert on the code, not the
    prose. (Failures from the models' own validators keep matching their own
    message text, which this project owns and controls.)

    ``at`` additionally requires the error to point at that field, which is what
    makes a typo test evidence: without it, ``extra_forbidden`` raised for some
    *other* key in the same payload would satisfy the assertion just as well.
    """
    errors = exc_info.value.errors()
    actual = [err["type"] for err in errors]
    for expected in expected_types:
        if at is None:
            assert expected in actual, f"expected pydantic error type {expected!r}, got {actual}"
        else:
            located = [err for err in errors if err["type"] == expected and at in err["loc"]]
            assert located, f"expected pydantic error {expected!r} at {at!r}, got {[(e['type'], e['loc']) for e in errors]}"


def make_request(url: str = "https://example.com", **overrides: Any) -> Request:
    """A minimal valid Request; override any field via keyword."""
    return Request(url=url, **overrides)


def make_stage(name: str = "test", request: Request | None = None, **overrides: Any) -> Stage:
    """A minimal valid Stage with an incidental request; override any field.

    Pass ``request=make_request(...)`` when the request itself is what matters.
    """
    return Stage(name=name, request=request if request is not None else make_request(), **overrides)


def stage_dict(**overrides: Any) -> dict[str, Any]:
    """Raw-dict form of a minimal Stage for ``Stage.model_validate(...)`` tests.

    Override or inject (including deliberately invalid) keys via keyword.
    """
    return {"name": "test", "request": {"url": "https://example.com"}, **overrides}
