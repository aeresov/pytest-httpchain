"""Unit tests for Substitutions and Responses flexible input formats.

Both accept a list or a name-keyed mapping; a mapping flattens to a list in key
order, a list value extended in place.
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import (
    FunctionsSubstitution,
    JMESPathSave,
    SaveStep,
    Stage,
    UserFunctionName,
    VarsSubstitution,
    Verify,
    VerifyStep,
)
from tests.unit.models.helpers import assert_error_types, stage_dict

V1 = VarsSubstitution(vars={"key1": "value1"})
V2 = VarsSubstitution(vars={"key2": "value2"})
V3 = VarsSubstitution(vars={"key3": "value3"})
FN = FunctionsSubstitution(functions={"timestamp": UserFunctionName("utils:get_timestamp")})


@pytest.mark.parametrize(
    ("substitutions", "expected"),
    [
        pytest.param([], [], id="empty-list"),
        pytest.param({}, [], id="empty-mapping"),
        pytest.param([{"vars": {"key1": "value1"}}, {"vars": {"key2": "value2"}}], [V1, V2], id="list"),
        pytest.param([V1, FN], [V1, FN], id="list-of-models"),
        pytest.param({"first": {"vars": {"key1": "value1"}}, "second": {"vars": {"key2": "value2"}}}, [V1, V2], id="mapping"),
        pytest.param(
            {"batch1": [{"vars": {"key1": "value1"}}, {"vars": {"key2": "value2"}}], "batch2": {"vars": {"key3": "value3"}}},
            [V1, V2, V3],
            id="mapping-with-list-value",
        ),
        pytest.param(
            {"initial_data": {"vars": {"key1": "value1"}}, "computed_values": {"functions": {"timestamp": "utils:get_timestamp"}}},
            [V1, FN],
            id="mapping-mixed-kinds",
        ),
    ],
)
def test_substitutions_input_forms(substitutions, expected):
    assert Stage.model_validate(stage_dict(substitutions=substitutions)).substitutions == expected


VERIFY_200 = VerifyStep(verify=Verify(status=200))
VERIFY_JSON = VerifyStep(verify=Verify(headers={"Content-Type": "application/json"}))
SAVE_RESULT = SaveStep(save=JMESPathSave(jmespath={"result": "data"}))


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        pytest.param([], [], id="empty-list"),
        pytest.param({}, [], id="empty-mapping"),
        pytest.param([{"verify": {"status": 200}}, {"save": {"jmespath": {"result": "data"}}}], [VERIFY_200, SAVE_RESULT], id="list"),
        pytest.param([VERIFY_200, SAVE_RESULT], [VERIFY_200, SAVE_RESULT], id="list-of-models"),
        pytest.param(
            {"verify_success": {"verify": {"status": 200}}, "save_result": {"save": {"jmespath": {"result": "data"}}}},
            [VERIFY_200, SAVE_RESULT],
            id="mapping",
        ),
        pytest.param(
            {
                "validations": [{"verify": {"status": 200}}, {"verify": {"headers": {"Content-Type": "application/json"}}}],
                "extraction": {"save": {"jmespath": {"result": "data"}}},
            },
            [VERIFY_200, VERIFY_JSON, SAVE_RESULT],
            id="mapping-with-list-value",
        ),
    ],
)
def test_responses_input_forms(response, expected):
    assert Stage.model_validate(stage_dict(response=response)).response == expected


@pytest.mark.parametrize("field", ["substitutions", "response"])
def test_non_list_non_mapping_rejected(field):
    with pytest.raises(ValidationError) as exc_info:
        Stage.model_validate(stage_dict(**{field: "invalid"}))
    assert_error_types(exc_info, "list_type", at=field)


def test_vars_values_become_namespaces_recursively():
    """So ``{{ user.roles[0].id }}`` attribute access works in templates."""
    substitution = VarsSubstitution(vars={"user": {"name": "a", "roles": [{"id": 1}]}})
    assert substitution.vars == {"user": SimpleNamespace(name="a", roles=[SimpleNamespace(id=1)])}


@pytest.mark.parametrize(
    "construct",
    [
        pytest.param(lambda: VarsSubstitution(vars={"my-var": "value"}), id="vars"),
        pytest.param(lambda: FunctionsSubstitution(functions={"my-func": UserFunctionName("mod:func")}), id="functions"),
    ],
)
def test_substitution_key_must_be_identifier(construct):
    """M24: substitution keys become context variable names, so a key that is
    not a valid Python identifier can never be referenced in a {{ }} expression.
    (Wiring only: the identifier rules live in test_type_validators.py.)"""
    with pytest.raises(ValidationError, match="Invalid Python variable name"):
        construct()
