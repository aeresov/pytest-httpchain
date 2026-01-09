"""Unit tests for Save types: JMESPathSave, SubstitutionsSave, UserFunctionsSave."""

import pytest
from pydantic import ValidationError
from pytest_httpchain_models.entities import (
    FunctionsSubstitution,
    JMESPathSave,
    Request,
    SaveStep,
    Stage,
    SubstitutionsSave,
    UserFunctionKwargs,
    UserFunctionName,
    UserFunctionsSave,
    VarsSubstitution,
)


class TestJMESPathSave:
    """Tests for JMESPathSave model."""

    def test_jmespath_simple_expression(self):
        """Test JMESPathSave with simple expression."""
        save = JMESPathSave(jmespath={"result": "data.value"})
        assert save.jmespath == {"result": "data.value"}

    def test_jmespath_multiple_expressions(self):
        """Test JMESPathSave with multiple expressions."""
        save = JMESPathSave(
            jmespath={
                "user_id": "data.user.id",
                "user_name": "data.user.name",
                "items": "data.items[*].id",
            }
        )
        assert len(save.jmespath) == 3

    def test_jmespath_complex_expressions(self):
        """Test JMESPathSave with complex JMESPath expressions."""
        save = JMESPathSave(
            jmespath={
                "active_users": "users[?active == `true`].name",
                "first_item": "items | [0]",
            }
        )
        assert "active_users" in save.jmespath

    def test_jmespath_with_template(self):
        """Test JMESPathSave with template expression."""
        save = JMESPathSave(jmespath={"result": "{{ jmespath_expr }}"})
        assert save.jmespath["result"] == "{{ jmespath_expr }}"

    def test_jmespath_invalid_expression_rejected(self):
        """Test that invalid JMESPath expressions are rejected."""
        with pytest.raises(ValidationError, match="Invalid JMESPath expression"):
            JMESPathSave(jmespath={"result": "[invalid"})

    def test_jmespath_with_description(self):
        """Test JMESPathSave with description."""
        save = JMESPathSave(
            jmespath={"token": "data.token"},
            description="Extract authentication token",
        )
        assert save.description == "Extract authentication token"

    def test_jmespath_extra_fields_forbidden(self):
        """Test that extra fields are not allowed."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            JMESPathSave(jmespath={"x": "y"}, extra="field")  # type: ignore[call-arg]


class TestSubstitutionsSave:
    """Tests for SubstitutionsSave model."""

    def test_substitutions_with_vars(self):
        """Test SubstitutionsSave with vars substitution."""
        save = SubstitutionsSave(substitutions=[VarsSubstitution(vars={"saved_value": "extracted"})])
        assert len(save.substitutions) == 1

    def test_substitutions_with_functions(self):
        """Test SubstitutionsSave with functions substitution."""
        save = SubstitutionsSave(substitutions=[FunctionsSubstitution(functions={"processor": UserFunctionName("utils:process_data")})])
        assert len(save.substitutions) == 1

    def test_substitutions_mixed(self):
        """Test SubstitutionsSave with mixed substitutions."""
        save = SubstitutionsSave(
            substitutions=[
                VarsSubstitution(vars={"constant": "value"}),
                FunctionsSubstitution(functions={"computed": UserFunctionName("module:compute")}),
            ]
        )
        assert len(save.substitutions) == 2

    def test_substitutions_dict_format(self):
        """Test SubstitutionsSave with dict format."""
        save = SubstitutionsSave(
            substitutions={  # type: ignore[arg-type]
                "constants": {"vars": {"x": 1}},
                "computed": {"functions": {"y": "mod:func"}},
            }
        )
        assert len(save.substitutions) == 2

    def test_substitutions_with_description(self):
        """Test SubstitutionsSave with description."""
        save = SubstitutionsSave(
            substitutions=[VarsSubstitution(vars={"x": 1})],
            description="Save computed values",
        )
        assert save.description == "Save computed values"

    def test_substitutions_extra_fields_forbidden(self):
        """Test that extra fields are not allowed."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            SubstitutionsSave(substitutions=[], extra="field")  # type: ignore[call-arg]


class TestUserFunctionsSave:
    """Tests for UserFunctionsSave model."""

    def test_user_functions_simple(self):
        """Test UserFunctionsSave with simple function."""
        save = UserFunctionsSave(user_functions=[UserFunctionName("module:save_data")])
        assert len(save.user_functions) == 1

    def test_user_functions_multiple(self):
        """Test UserFunctionsSave with multiple functions."""
        save = UserFunctionsSave(
            user_functions=[
                UserFunctionName("validators:check_response"),
                UserFunctionName("storage:save_to_db"),
            ]
        )
        assert len(save.user_functions) == 2

    def test_user_functions_with_kwargs(self):
        """Test UserFunctionsSave with function kwargs."""
        save = UserFunctionsSave(
            user_functions=[
                UserFunctionKwargs(
                    name=UserFunctionName("module:process"),
                    kwargs={"format": "json", "validate": True},
                )
            ]
        )
        assert len(save.user_functions) == 1

    def test_user_functions_mixed(self):
        """Test UserFunctionsSave with mixed formats."""
        save = UserFunctionsSave(
            user_functions=[
                UserFunctionName("simple:func"),
                UserFunctionKwargs(
                    name=UserFunctionName("complex:func"),
                    kwargs={"arg": "value"},
                ),
            ]
        )
        assert len(save.user_functions) == 2

    def test_user_functions_with_description(self):
        """Test UserFunctionsSave with description."""
        save = UserFunctionsSave(
            user_functions=[UserFunctionName("module:func")],
            description="Custom save handler",
        )
        assert save.description == "Custom save handler"

    def test_user_functions_extra_fields_forbidden(self):
        """Test that extra fields are not allowed."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            UserFunctionsSave(user_functions=[], extra="field")  # type: ignore[call-arg]


class TestSaveDiscriminator:
    """Tests for Save discriminated union."""

    def test_discriminator_jmespath(self):
        """Test discriminator identifies JMESPathSave."""
        step = SaveStep(save=JMESPathSave(jmespath={"result": "data"}))
        assert isinstance(step.save, JMESPathSave)

    def test_discriminator_substitutions(self):
        """Test discriminator identifies SubstitutionsSave."""
        step = SaveStep(save=SubstitutionsSave(substitutions=[VarsSubstitution(vars={"x": 1})]))
        assert isinstance(step.save, SubstitutionsSave)

    def test_discriminator_user_functions(self):
        """Test discriminator identifies UserFunctionsSave."""
        step = SaveStep(save=UserFunctionsSave(user_functions=[UserFunctionName("module:func")]))
        assert isinstance(step.save, UserFunctionsSave)

    def test_discriminator_invalid_rejected(self):
        """Test that invalid save type is rejected."""
        with pytest.raises(ValueError, match="Unable to determine save type"):
            SaveStep(save={"invalid": "value"})  # type: ignore[arg-type]


class TestSaveStepInStage:
    """Tests for SaveStep in Stage response."""

    def test_stage_with_jmespath_save(self):
        """Test Stage with JMESPath save step."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            response=[SaveStep(save=JMESPathSave(jmespath={"token": "data.token"}))],
        )
        assert len(stage.response) == 1
        assert isinstance(stage.response[0], SaveStep)
        assert isinstance(stage.response[0].save, JMESPathSave)

    def test_stage_with_multiple_save_steps(self):
        """Test Stage with multiple save steps."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            response=[
                SaveStep(save=JMESPathSave(jmespath={"id": "data.id"})),
                SaveStep(save=UserFunctionsSave(user_functions=[UserFunctionName("custom:saver")])),
            ],
        )
        assert len(stage.response) == 2
        assert all(isinstance(s, SaveStep) for s in stage.response)
