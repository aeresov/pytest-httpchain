"""Shared factories for the model unit tests.

Most of these tests only need a *valid, incidental* Stage or Request as
scaffolding while they exercise one specific field. These helpers provide that
minimal object so a call site can pass just the field under test instead of
re-spelling the whole ``Stage(name=..., request=Request(url=...), ...)``
boilerplate. Import them directly, mirroring how the integration suite imports
``run_scenario`` from its conftest::

    from tests.unit.models.conftest import make_stage, make_request
"""

from typing import Any

from pytest_httpchain.models import Request, Stage


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
