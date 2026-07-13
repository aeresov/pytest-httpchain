"""Every model rejects unknown keys, so typos fail validation instead of silently changing behavior."""

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import (
    CombinationsParameter,
    IndividualParameter,
    Request,
    ResponseBody,
    Scenario,
    SSLConfig,
    Stage,
    UserFunctionKwargs,
    Verify,
)


@pytest.mark.parametrize(
    ("model", "data", "typo"),
    [
        (SSLConfig, {"verify": True, "verfy": False}, "verfy"),
        (UserFunctionKwargs, {"name": "mod:func", "kwarg": {}}, "kwarg"),
        (Request, {"url": "https://x.test/", "headerz": {}}, "headerz"),
        (Request, {"url": "https://x.test/", "param": {}}, "param"),
        (ResponseBody, {"containz": ["x"]}, "containz"),
        (Verify, {"statu": 200}, "statu"),
        (IndividualParameter, {"individual": {"n": [1]}, "idz": ["a"]}, "idz"),
        (CombinationsParameter, {"combinations": [{"n": 1}], "idz": ["a"]}, "idz"),
        (
            Stage,
            {"name": "s", "alwaysrun": True, "request": {"url": "https://x.test/"}},
            "alwaysrun",
        ),
        (
            Scenario,
            {"vars": {"a": 1}, "stages": []},
            "vars",
        ),
    ],
)
def test_unknown_key_rejected(model, data, typo):
    with pytest.raises(ValidationError) as exc_info:
        model.model_validate(data)
    errors = exc_info.value.errors()
    assert any(e["type"] == "extra_forbidden" and typo in e["loc"] for e in errors)


def test_parallel_config_extra_key_rejected():
    """Extra keys are rejected on both parallel variants (incl. base-class fields)."""
    stage = {
        "name": "s",
        "request": {"url": "https://x.test/"},
        "parallel": {"repeat": 2, "max_concurency": 5},
    }
    with pytest.raises(ValidationError, match="max_concurency"):
        Stage.model_validate(stage)


def test_schema_key_dropped_at_every_model_position():
    """ "$schema" is editor metadata: a file (or a referenced fragment landing at
    a model position) may carry it at its root without failing validation."""
    scenario = Scenario.model_validate(
        {
            "$schema": "https://aeresov.github.io/pytest-httpchain/schema/scenario.schema.json",
            "stages": [
                {
                    "$schema": "https://example.test/stage-fragment.schema.json",
                    "name": "s",
                    "request": {
                        "$schema": "https://example.test/request-fragment.schema.json",
                        "url": "https://x.test/",
                        "body": {"$schema": "https://example.test/body.schema.json", "json": {"a": 1}},
                    },
                    "response": [{"$schema": "https://example.test/step.schema.json", "verify": {"$schema": "https://example.test/v.schema.json", "status": 200}}],
                }
            ],
        }
    )
    assert scenario.stages[0].request.body.json == {"a": 1}


def test_schema_key_preserved_inside_plain_dict_values():
    """A "$schema" inside a VALUE (inline JSON Schema, json body) is content."""
    verify = Verify.model_validate({"body": {"schema": {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}}})
    assert verify.body.schema["$schema"] == "http://json-schema.org/draft-07/schema#"

    request = Request.model_validate({"url": "https://x.test/", "body": {"json": {"$schema": "kept", "x": 1}}})
    assert request.body.json == {"$schema": "kept", "x": 1}
