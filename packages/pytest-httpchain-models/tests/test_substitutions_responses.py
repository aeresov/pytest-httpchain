"""Unit tests for Substitutions and Responses flexible input formats."""

from http import HTTPStatus

import pytest
from pydantic import ValidationError
from pytest_httpchain_models.entities import (
    FunctionsSubstitution,
    JMESPathSave,
    Request,
    SaveStep,
    Scenario,
    Stage,
    UserFunctionName,
    VarsSubstitution,
    Verify,
    VerifyStep,
)


class TestSubstitutionsListFormat:
    """Tests for Substitutions with list format."""

    def test_substitutions_empty_list(self):
        """Test Substitutions with empty list."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            substitutions=[],
        )
        assert stage.substitutions == []

    def test_substitutions_single_vars_item(self):
        """Test Substitutions with single vars item."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            substitutions=[VarsSubstitution(vars={"key1": "value1"})],
        )
        assert len(stage.substitutions) == 1
        sub0 = stage.substitutions[0]
        assert isinstance(sub0, VarsSubstitution)
        assert sub0.vars == {"key1": "value1"}

    def test_substitutions_multiple_vars_items(self):
        """Test Substitutions with multiple vars items."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            substitutions=[
                VarsSubstitution(vars={"key1": "value1"}),
                VarsSubstitution(vars={"key2": "value2"}),
            ],
        )
        assert len(stage.substitutions) == 2
        assert all(isinstance(s, VarsSubstitution) for s in stage.substitutions)
        sub0, sub1 = stage.substitutions[0], stage.substitutions[1]
        assert isinstance(sub0, VarsSubstitution) and isinstance(sub1, VarsSubstitution)
        assert sub0.vars == {"key1": "value1"}
        assert sub1.vars == {"key2": "value2"}

    def test_substitutions_mixed_vars_and_functions(self):
        """Test Substitutions with mixed vars and functions."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            substitutions=[
                VarsSubstitution(vars={"id": 42}),
                FunctionsSubstitution(functions={"timestamp": UserFunctionName("utils:get_timestamp")}),
            ],
        )
        assert len(stage.substitutions) == 2
        assert isinstance(stage.substitutions[0], VarsSubstitution)
        assert isinstance(stage.substitutions[1], FunctionsSubstitution)


class TestSubstitutionsDictFormat:
    """Tests for Substitutions with dictionary format."""

    def test_substitutions_empty_dict(self):
        """Test Substitutions with empty dict."""
        stage = Stage.model_validate(
            {
                "name": "test",
                "request": {"url": "https://example.com"},
                "substitutions": {},
            }
        )
        assert stage.substitutions == []

    def test_substitutions_dict_single_item(self):
        """Test Substitutions with dict containing single item."""
        stage = Stage.model_validate(
            {
                "name": "test",
                "request": {"url": "https://example.com"},
                "substitutions": {"initial": {"vars": {"key1": "value1"}}},
            }
        )
        assert len(stage.substitutions) == 1
        sub0 = stage.substitutions[0]
        assert isinstance(sub0, VarsSubstitution)
        assert sub0.vars == {"key1": "value1"}

    def test_substitutions_dict_multiple_items(self):
        """Test Substitutions with dict containing multiple items."""
        stage = Stage.model_validate(
            {
                "name": "test",
                "request": {"url": "https://example.com"},
                "substitutions": {
                    "first": {"vars": {"key1": "value1"}},
                    "second": {"vars": {"key2": "value2"}},
                },
            }
        )
        assert len(stage.substitutions) == 2
        assert all(isinstance(s, VarsSubstitution) for s in stage.substitutions)
        # Note: dict order is preserved in Python 3.7+
        vars_dict = {k: v for s in stage.substitutions if isinstance(s, VarsSubstitution) for k, v in s.vars.items()}
        assert "key1" in vars_dict
        assert "key2" in vars_dict

    def test_substitutions_dict_with_list_values(self):
        """Test Substitutions with dict containing list values (flattened)."""
        stage = Stage.model_validate(
            {
                "name": "test",
                "request": {"url": "https://example.com"},
                "substitutions": {
                    "batch1": [
                        {"vars": {"key1": "value1"}},
                        {"vars": {"key2": "value2"}},
                    ],
                    "batch2": {"vars": {"key3": "value3"}},
                },
            }
        )
        # Expects 3 items total: batch1 list is extended (2 items), batch2 is appended (1 item)
        assert len(stage.substitutions) == 3
        assert all(isinstance(s, VarsSubstitution) for s in stage.substitutions)

    def test_substitutions_dict_mixed_vars_and_functions(self):
        """Test Substitutions dict with mixed vars and functions."""
        stage = Stage.model_validate(
            {
                "name": "test",
                "request": {"url": "https://example.com"},
                "substitutions": {
                    "initial_data": {"vars": {"id": 42}},
                    "computed_values": {"functions": {"timestamp": "utils:get_timestamp"}},
                },
            }
        )
        assert len(stage.substitutions) == 2
        # Find the VarsSubstitution and FunctionsSubstitution
        vars_subs = [s for s in stage.substitutions if isinstance(s, VarsSubstitution)]
        funcs_subs = [s for s in stage.substitutions if isinstance(s, FunctionsSubstitution)]
        assert len(vars_subs) == 1
        assert len(funcs_subs) == 1


class TestResponsesListFormat:
    """Tests for Responses with list format."""

    def test_responses_empty_list(self):
        """Test Responses with empty list."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            response=[],
        )
        assert stage.response == []

    def test_responses_single_save_step(self):
        """Test Responses with single save step."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            response=[SaveStep(save=JMESPathSave(jmespath={"result": "data.value"}))],
        )
        assert len(stage.response) == 1
        assert isinstance(stage.response[0], SaveStep)

    def test_responses_single_verify_step(self):
        """Test Responses with single verify step."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            response=[VerifyStep(verify=Verify(status=HTTPStatus.OK))],
        )
        assert len(stage.response) == 1
        assert isinstance(stage.response[0], VerifyStep)

    def test_responses_multiple_steps(self):
        """Test Responses with multiple steps."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            response=[
                VerifyStep(verify=Verify(status=HTTPStatus.OK)),
                SaveStep(save=JMESPathSave(jmespath={"result": "data"})),
            ],
        )
        assert len(stage.response) == 2
        assert isinstance(stage.response[0], VerifyStep)
        assert isinstance(stage.response[1], SaveStep)


class TestResponsesDictFormat:
    """Tests for Responses with dictionary format."""

    def test_responses_empty_dict(self):
        """Test Responses with empty dict."""
        stage = Stage.model_validate(
            {
                "name": "test",
                "request": {"url": "https://example.com"},
                "response": {},
            }
        )
        assert stage.response == []

    def test_responses_dict_single_item(self):
        """Test Responses with dict containing single item."""
        stage = Stage.model_validate(
            {
                "name": "test",
                "request": {"url": "https://example.com"},
                "response": {"check_status": {"verify": {"status": 200}}},
            }
        )
        assert len(stage.response) == 1
        assert isinstance(stage.response[0], VerifyStep)

    def test_responses_dict_multiple_items(self):
        """Test Responses with dict containing multiple items."""
        stage = Stage.model_validate(
            {
                "name": "test",
                "request": {"url": "https://example.com"},
                "response": {
                    "verify_success": {"verify": {"status": 200}},
                    "save_result": {"save": {"jmespath": {"data": "response"}}},
                },
            }
        )
        assert len(stage.response) == 2

    def test_responses_dict_with_list_values(self):
        """Test Responses with dict containing list values (flattened)."""
        stage = Stage.model_validate(
            {
                "name": "test",
                "request": {"url": "https://example.com"},
                "response": {
                    "validations": [
                        {"verify": {"status": 200}},
                        {"verify": {"headers": {"Content-Type": "application/json"}}},
                    ],
                    "extraction": {"save": {"jmespath": {"result": "data"}}},
                },
            }
        )
        # Expects 3 items total: validations list extended (2), extraction appended (1)
        assert len(stage.response) == 3


class TestSchemaGeneration:
    """Tests for JSON schema generation."""

    def test_substitutions_schema_includes_array_type(self):
        """Test that Substitutions schema includes array type."""
        schema = Stage.model_json_schema()
        subs_schema = schema["properties"]["substitutions"]

        # Should have anyOf with array type
        assert "anyOf" in subs_schema
        types = [s.get("type") for s in subs_schema["anyOf"]]
        assert "array" in types

    def test_substitutions_schema_includes_object_type(self):
        """Test that Substitutions schema includes object type."""
        schema = Stage.model_json_schema()
        subs_schema = schema["properties"]["substitutions"]

        # Should have anyOf with object type
        assert "anyOf" in subs_schema
        types = [s.get("type") for s in subs_schema["anyOf"]]
        assert "object" in types

    def test_substitutions_schema_object_has_additional_properties(self):
        """Test that object type in Substitutions schema defines additionalProperties."""
        schema = Stage.model_json_schema()
        subs_schema = schema["properties"]["substitutions"]

        # Find the object type definition
        object_schemas = [s for s in subs_schema["anyOf"] if s.get("type") == "object"]
        assert len(object_schemas) == 1
        assert "additionalProperties" in object_schemas[0]

    def test_responses_schema_includes_array_type(self):
        """Test that Responses schema includes array type."""
        schema = Stage.model_json_schema()
        resp_schema = schema["properties"]["response"]

        # Should have anyOf with array type
        assert "anyOf" in resp_schema
        types = [s.get("type") for s in resp_schema["anyOf"]]
        assert "array" in types

    def test_responses_schema_includes_object_type(self):
        """Test that Responses schema includes object type."""
        schema = Stage.model_json_schema()
        resp_schema = schema["properties"]["response"]

        # Should have anyOf with object type
        assert "anyOf" in resp_schema
        types = [s.get("type") for s in resp_schema["anyOf"]]
        assert "object" in types

    def test_scenario_substitutions_schema(self):
        """Test that Scenario-level substitutions also has correct schema."""
        schema = Scenario.model_json_schema()
        subs_schema = schema["properties"]["substitutions"]

        # Should have anyOf with both array and object
        assert "anyOf" in subs_schema
        types = [s.get("type") for s in subs_schema["anyOf"]]
        assert "array" in types
        assert "object" in types


class TestIntegrationWithScenario:
    """Integration tests with complete Scenario."""

    def test_scenario_with_list_substitutions(self):
        """Test Scenario with list-format substitutions."""
        scenario = Scenario(
            substitutions=[VarsSubstitution(vars={"base_url": "https://api.example.com"})],
            stages=[
                Stage(
                    name="test",
                    request=Request(url="{{ base_url }}/endpoint"),
                )
            ],
        )
        assert len(scenario.substitutions) == 1
        assert isinstance(scenario.substitutions[0], VarsSubstitution)

    def test_scenario_with_dict_substitutions(self):
        """Test Scenario with dict-format substitutions."""
        scenario = Scenario.model_validate(
            {
                "substitutions": {"config": {"vars": {"base_url": "https://api.example.com"}}},
                "stages": [
                    {
                        "name": "test",
                        "request": {"url": "{{ base_url }}/endpoint"},
                    }
                ],
            }
        )
        assert len(scenario.substitutions) == 1
        assert isinstance(scenario.substitutions[0], VarsSubstitution)

    def test_stage_with_list_responses(self):
        """Test Stage with list-format responses."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            response=[VerifyStep(verify=Verify(status=HTTPStatus.OK))],
        )
        assert len(stage.response) == 1
        assert isinstance(stage.response[0], VerifyStep)

    def test_stage_with_dict_responses(self):
        """Test Stage with dict-format responses."""
        stage = Stage.model_validate(
            {
                "name": "test",
                "request": {"url": "https://example.com"},
                "response": {"validation": {"verify": {"status": 200}}},
            }
        )
        assert len(stage.response) == 1
        assert isinstance(stage.response[0], VerifyStep)


class TestInvalidInputs:
    """Tests for invalid inputs."""

    def test_substitutions_invalid_type(self):
        """Test that invalid types are rejected."""
        with pytest.raises(ValidationError):
            Stage(
                name="test",
                request=Request(url="https://example.com"),
                substitutions="invalid",  # type: ignore[arg-type]
            )

    def test_substitutions_invalid_item_structure(self):
        """Test that items with invalid structure are rejected."""
        with pytest.raises(ValueError, match="Unable to determine substitution type"):
            Stage.model_validate(
                {
                    "name": "test",
                    "request": {"url": "https://example.com"},
                    "substitutions": [{"invalid_key": "value"}],
                }
            )

    def test_responses_invalid_type(self):
        """Test that invalid response types are rejected."""
        with pytest.raises(ValidationError):
            Stage(
                name="test",
                request=Request(url="https://example.com"),
                response="invalid",  # type: ignore[arg-type]
            )

    def test_responses_invalid_item_structure(self):
        """Test that response items with invalid structure are rejected."""
        with pytest.raises(ValueError, match="Unable to determine step type"):
            Stage.model_validate(
                {
                    "name": "test",
                    "request": {"url": "https://example.com"},
                    "response": [{"invalid_key": "value"}],
                }
            )
