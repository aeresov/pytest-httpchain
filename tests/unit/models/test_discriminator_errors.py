"""Discriminated-union failures must be Pydantic ValidationErrors, not bare ValueError.

A bare ``ValueError`` raised inside a discriminator is *not* a ``ValidationError``,
so it escapes every ``except ValidationError`` handler in the validator CLI,
collection, and the show/graph inspection commands, surfacing as a raw traceback.
Returning an unrecognized tag makes Pydantic raise a clean, located
``union_tag_invalid`` ValidationError that those handlers catch.
"""

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import Request, SaveStep, Stage
from tests.unit.models.helpers import assert_error_types, stage_dict

# (label, callable that should raise a ValidationError on a malformed shape, field it names)
CASES = [
    ("body: unknown key", lambda: Request(url="https://example.com", method="POST", body={"jsonn": {"a": 1}}), "body"),
    ("body: empty object", lambda: Request(url="https://example.com", method="POST", body={}), "body"),
    ("body: not an object", lambda: Request(url="https://example.com", method="POST", body="raw"), "body"),
    ("save: unknown key", lambda: SaveStep(save={"invalid": "value"}), "save"),
    ("parallel: unknown key", lambda: Stage.model_validate(stage_dict(parallel={"nope": 1})), "parallel"),
    ("substitution: unknown key", lambda: Stage.model_validate(stage_dict(substitutions=[{"invalid_key": "value"}])), "substitutions"),
    ("response step: unknown key", lambda: Stage.model_validate(stage_dict(response=[{"invalid_key": "value"}])), "response"),
    ("parameter step: unknown key", lambda: Stage.model_validate(stage_dict(parametrize=[{"invalid": 1}])), "parametrize"),
]


@pytest.mark.parametrize(("label", "construct", "field"), CASES, ids=[c[0] for c in CASES])
def test_malformed_discriminated_shape_raises_validation_error(label, construct, field):
    """A malformed shape on any discriminated union raises a proper ValidationError
    (so the CLI/collection/inspection handlers catch it), not a bare ValueError —
    and it is the discriminator that rejected it, not some unrelated error."""
    with pytest.raises(ValidationError) as exc_info:
        construct()
    assert_error_types(exc_info, "union_tag_invalid", at=field)


def test_unknown_body_key_is_named_in_the_message():
    """The offending key is surfaced so the user can locate the typo."""
    with pytest.raises(ValidationError) as exc_info:
        Request(url="https://example.com", method="POST", body={"jsonn": {"a": 1}})
    msg = str(exc_info.value)
    assert "jsonn" in msg
    assert "body" in msg
