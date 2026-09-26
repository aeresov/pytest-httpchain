"""Every model rejects unknown keys, so typos fail validation instead of silently changing behavior."""

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import (
    Base64Body,
    BinaryBody,
    CombinationsParameter,
    FilesBody,
    FormBody,
    GraphQL,
    GraphQLBody,
    IndividualParameter,
    JMESPathSave,
    JsonBody,
    ParallelForeachConfig,
    ParallelRepeatConfig,
    Request,
    ResponseBody,
    Scenario,
    SSLConfig,
    Stage,
    SubstitutionsSave,
    TextBody,
    UserFunctionKwargs,
    UserFunctionsSave,
    Verify,
    XmlBody,
)
from tests.unit.models.helpers import assert_error_types


@pytest.mark.parametrize(
    ("model", "data", "typo"),
    [
        (SSLConfig, {"verify": True, "verfy": False}, "verfy"),
        (UserFunctionKwargs, {"name": "mod:func", "kwarg": {}}, "kwarg"),
        (Request, {"url": "https://x.test/", "headerz": {}}, "headerz"),
        (Request, {"url": "https://x.test/", "param": {}}, "param"),
        (JsonBody, {"json": {"key": "value"}, "extra": "field"}, "extra"),
        (XmlBody, {"xml": "<root/>", "extra": "field"}, "extra"),
        (FormBody, {"form": {"key": "value"}, "extra": "field"}, "extra"),
        (TextBody, {"text": "content", "extra": "field"}, "extra"),
        (Base64Body, {"base64": "dGVzdA==", "extra": "field"}, "extra"),
        (BinaryBody, {"binary": "file.bin", "extra": "field"}, "extra"),
        (FilesBody, {"files": {"f": "file.txt"}, "extra": "field"}, "extra"),
        (GraphQL, {"query": "{ test }", "extra": "field"}, "extra"),
        (GraphQLBody, {"graphql": {"query": "{ test }"}, "extra": "field"}, "extra"),
        (ResponseBody, {"containz": ["x"]}, "containz"),
        (Verify, {"statu": 200}, "statu"),
        # A header matcher object, reached through Verify's str | HeaderMatcher union.
        (Verify, {"headers": {"content-type": {"equals": "x"}}}, "equals"),
        (JMESPathSave, {"jmespath": {"x": "y"}, "extra": "field"}, "extra"),
        (SubstitutionsSave, {"substitutions": [], "extra": "field"}, "extra"),
        (UserFunctionsSave, {"user_functions": [], "extra": "field"}, "extra"),
        (IndividualParameter, {"individual": {"n": [1]}, "idz": ["a"]}, "idz"),
        (CombinationsParameter, {"combinations": [{"n": 1}], "idz": ["a"]}, "idz"),
        (ParallelRepeatConfig, {"repeat": 10, "extra": "field"}, "extra"),
        (ParallelForeachConfig, {"foreach": [{"individual": {"x": [1]}}], "extra": "field"}, "extra"),
        # A base-class field typo, on the variant the Stage discriminator picked.
        (Stage, {"name": "s", "request": {"url": "https://x.test/"}, "parallel": {"repeat": 2, "max_concurency": 5}}, "max_concurency"),
        (Stage, {"name": "s", "alwaysrun": True, "request": {"url": "https://x.test/"}}, "alwaysrun"),
        (Scenario, {"vars": {"a": 1}, "stages": []}, "vars"),
    ],
    ids=lambda v: v.__name__ if isinstance(v, type) else None,
)
def test_unknown_key_rejected(model, data, typo):
    with pytest.raises(ValidationError) as exc_info:
        model.model_validate(data)
    assert_error_types(exc_info, "extra_forbidden", at=typo)


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
