"""Unit tests for Save types: JMESPathSave, SubstitutionsSave, UserFunctionsSave."""

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import (
    FunctionsSubstitution,
    JMESPathSave,
    SaveStep,
    SubstitutionsSave,
    UserFunctionKwargs,
    UserFunctionName,
    UserFunctionsSave,
    VarsSubstitution,
)


@pytest.mark.parametrize(
    ("save", "expected"),
    [
        pytest.param({"jmespath": {"result": "data"}}, JMESPathSave, id="jmespath"),
        pytest.param({"substitutions": [{"vars": {"x": 1}}]}, SubstitutionsSave, id="substitutions"),
        pytest.param({"user_functions": ["module:func"]}, UserFunctionsSave, id="user_functions"),
    ],
)
def test_raw_save_dict_selects_model(save, expected):
    assert type(SaveStep.model_validate({"save": save}).save) is expected


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        pytest.param(JMESPathSave, {"jmespath": {"token": "data.token"}}, id="jmespath"),
        pytest.param(SubstitutionsSave, {"substitutions": []}, id="substitutions"),
        pytest.param(UserFunctionsSave, {"user_functions": []}, id="user_functions"),
    ],
)
def test_description_round_trips(model, kwargs):
    assert model(**kwargs, description="Extract authentication token").description == "Extract authentication token"


class TestJMESPathSave:
    @pytest.mark.parametrize(
        "jmespath",
        [
            pytest.param({"user_id": "data.user.id", "user_name": "data.user.name", "items": "data.items[*].id"}, id="concrete"),
            pytest.param({"result": "{{ jmespath_expr }}"}, id="template"),
        ],
    )
    def test_expressions_round_trip(self, jmespath):
        assert JMESPathSave(jmespath=jmespath).jmespath == jmespath

    def test_invalid_expression_rejected(self):
        with pytest.raises(ValidationError, match="Invalid JMESPath expression"):
            JMESPathSave(jmespath={"result": "[invalid"})

    def test_key_must_be_identifier(self):
        """M24: save keys become context variable names, so a key that is not a
        valid Python identifier can never be referenced in a {{ }} expression.
        (Wiring only: the identifier rules live in test_type_validators.py.)"""
        with pytest.raises(ValidationError, match="Invalid Python variable name"):
            JMESPathSave(jmespath={"my-var": "data.value"})


def test_substitutions_save_normalizes_mapping_form():
    """Same ``Substitutions`` type as Stage/Scenario (list-or-mapping input)."""
    save = SubstitutionsSave.model_validate(
        {"substitutions": {"constants": {"vars": {"x": 1}}, "computed": {"functions": {"y": "mod:func"}}}},
    )
    assert save.substitutions == [VarsSubstitution(vars={"x": 1}), FunctionsSubstitution(functions={"y": UserFunctionName("mod:func")})]


def test_user_functions_save_accepts_name_and_kwargs_forms():
    save = UserFunctionsSave.model_validate({"user_functions": ["simple:func", {"name": "complex:func", "kwargs": {"arg": "value"}}]})
    assert save.user_functions == [
        UserFunctionName("simple:func"),
        UserFunctionKwargs(name=UserFunctionName("complex:func"), kwargs={"arg": "value"}),
    ]
