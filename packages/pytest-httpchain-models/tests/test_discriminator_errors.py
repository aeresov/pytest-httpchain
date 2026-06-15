"""Discriminated-union failures must be Pydantic ValidationErrors, not bare ValueError.

A bare ``ValueError`` raised inside a discriminator is *not* a ``ValidationError``,
so it escapes every ``except ValidationError`` handler in the validator CLI,
collection, and the show/graph inspection commands, surfacing as a raw traceback.
Returning an unrecognized tag makes Pydantic raise a clean, located
``union_tag_invalid`` ValidationError that those handlers catch.
"""

import pytest
from pydantic import ValidationError
from pytest_httpchain_models.entities import Request, SaveStep, Stage


def _req() -> Request:
    return Request(url="https://example.com")


# (label, callable that should raise a ValidationError on a malformed shape)
CASES = [
    ("body: unknown key", lambda: Request(url="https://example.com", method="POST", body={"jsonn": {"a": 1}})),
    ("body: empty object", lambda: Request(url="https://example.com", method="POST", body={})),
    ("body: not an object", lambda: Request(url="https://example.com", method="POST", body="raw")),
    ("save: unknown key", lambda: SaveStep(save={"invalid": "value"})),
    (
        "parallel: unknown key",
        lambda: Stage.model_validate({"name": "t", "request": {"url": "https://example.com"}, "parallel": {"nope": 1}}),
    ),
    (
        "substitution: unknown key",
        lambda: Stage.model_validate({"name": "t", "request": {"url": "https://example.com"}, "substitutions": [{"invalid_key": "value"}]}),
    ),
    (
        "response step: unknown key",
        lambda: Stage.model_validate({"name": "t", "request": {"url": "https://example.com"}, "response": [{"invalid_key": "value"}]}),
    ),
    (
        "parameter step: unknown key",
        lambda: Stage.model_validate({"name": "t", "request": {"url": "https://example.com"}, "parametrize": [{"invalid": 1}]}),
    ),
]


@pytest.mark.parametrize("label,construct", CASES, ids=[c[0] for c in CASES])
def test_malformed_discriminated_shape_raises_validation_error(label, construct):
    """A malformed shape on any discriminated union raises a proper ValidationError
    (so the CLI/collection/inspection handlers catch it), not a bare ValueError."""
    with pytest.raises(ValidationError) as exc_info:
        construct()
    # It is the discriminator that rejected it, not some unrelated error.
    assert any(e["type"] == "union_tag_invalid" for e in exc_info.value.errors()), exc_info.value.errors()


def test_unknown_body_key_is_named_in_the_message():
    """The offending key is surfaced so the user can locate the typo."""
    with pytest.raises(ValidationError) as exc_info:
        Request(url="https://example.com", method="POST", body={"jsonn": {"a": 1}})
    msg = str(exc_info.value)
    assert "jsonn" in msg
    assert "body" in msg
