import pytest
from pydantic import ValidationError

from pytest_http.models import Scenario


def test_scenario_with_stages():
    data = {"stages": [{"name": "stage1", "data": "data1"}, {"name": "stage2", "data": 42}]}
    scenario = Scenario.model_validate(data)
    assert len(scenario.stages) == 2
    assert scenario.stages[0].name == "stage1"
    assert scenario.stages[0].data == "data1"
    assert scenario.stages[1].name == "stage2"
    assert scenario.stages[1].data == 42


def test_scenario_empty():
    data = {}
    scenario = Scenario.model_validate(data)
    assert scenario.stages == []


def test_scenario_empty_stages():
    data = {"stages": []}
    scenario = Scenario.model_validate(data)
    assert scenario.stages == []


def test_scenario_with_extra_fields():
    data = {"stages": [{"name": "test", "data": 123}], "unknown_field": "should_be_ignored"}
    scenario = Scenario.model_validate(data)
    assert len(scenario.stages) == 1
    assert not hasattr(scenario, "unknown_field")


def test_scenario_invalid_stages_type():
    data = {"stages": "not_a_list"}
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)


def test_scenario_invalid_stage_structure():
    data = {"stages": [{"name": "valid_stage", "data": "data"}, {"invalid": "stage"}]}
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)


def test_scenario_with_complex_stages():
    data = {
        "stages": [
            {"name": "setup", "data": {"config": {"host": "localhost", "port": 8080}, "users": ["alice", "bob"]}},
            {"name": "action", "data": ["step1", "step2", "step3"]},
            {"name": "verification", "data": True},
        ]
    }
    scenario = Scenario.model_validate(data)
    assert len(scenario.stages) == 3
    assert scenario.stages[0].name == "setup"
    assert scenario.stages[0].data["config"]["host"] == "localhost"
    assert scenario.stages[1].data == ["step1", "step2", "step3"]
    assert scenario.stages[2].data is True


def test_scenario_with_valid_save_field():
    data = {
        "stages": [{"name": "test", "data": "data"}],
        "save": {"user_id": "user.id", "user_name": "user.name", "first_item": "items[0]", "_private_var": "data._private", "complex_path": "users[*].profile.name"},
    }
    scenario = Scenario.model_validate(data)
    assert scenario.save is not None
    assert scenario.save["user_id"] == "user.id"
    assert scenario.save["user_name"] == "user.name"
    assert scenario.save["first_item"] == "items[0]"
    assert scenario.save["_private_var"] == "data._private"
    assert scenario.save["complex_path"] == "users[*].profile.name"


def test_scenario_without_save_field():
    data = {"stages": [{"name": "test", "data": "data"}]}
    scenario = Scenario.model_validate(data)
    assert scenario.save is None


def test_scenario_with_empty_save_field():
    data = {"stages": [{"name": "test", "data": "data"}], "save": {}}
    scenario = Scenario.model_validate(data)
    assert scenario.save == {}


def test_scenario_with_save_field_none():
    data = {"stages": [{"name": "test", "data": "data"}], "save": None}
    scenario = Scenario.model_validate(data)
    assert scenario.save is None


def test_scenario_save_invalid_python_variable_name_starts_with_digit():
    data = {"stages": [{"name": "test", "data": "data"}], "save": {"1invalid": "user.id"}}
    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(data)
    assert "Key '1invalid' is not a valid Python variable name" in str(exc_info.value)


def test_scenario_save_invalid_python_variable_name_contains_special_chars():
    data = {"stages": [{"name": "test", "data": "data"}], "save": {"user-id": "user.id"}}
    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(data)
    assert "Key 'user-id' is not a valid Python variable name" in str(exc_info.value)


def test_scenario_save_invalid_python_variable_name_contains_space():
    data = {"stages": [{"name": "test", "data": "data"}], "save": {"user id": "user.id"}}
    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(data)
    assert "Key 'user id' is not a valid Python variable name" in str(exc_info.value)


def test_scenario_save_invalid_python_variable_name_special_symbols():
    data = {"stages": [{"name": "test", "data": "data"}], "save": {"user@id": "user.id"}}
    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(data)
    assert "Key 'user@id' is not a valid Python variable name" in str(exc_info.value)


def test_scenario_save_invalid_python_variable_name_empty_string():
    data = {"stages": [{"name": "test", "data": "data"}], "save": {"": "user.id"}}
    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(data)
    assert "Key '' is not a valid Python variable name" in str(exc_info.value)


def test_scenario_save_invalid_jmespath_expression():
    data = {"stages": [{"name": "test", "data": "data"}], "save": {"user_id": "user.[invalid}"}}
    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(data)
    assert "is not a valid JMESPath expression" in str(exc_info.value)


def test_scenario_save_invalid_jmespath_expression_incomplete():
    data = {"stages": [{"name": "test", "data": "data"}], "save": {"user_id": "user."}}
    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(data)
    assert "is not a valid JMESPath expression" in str(exc_info.value)


def test_scenario_save_invalid_jmespath_expression_unmatched_brackets():
    data = {"stages": [{"name": "test", "data": "data"}], "save": {"user_id": "users[0"}}
    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(data)
    assert "is not a valid JMESPath expression" in str(exc_info.value)


def test_scenario_save_valid_python_keywords_as_variable_names():
    # Note: isidentifier() returns True for Python keywords, but they're still valid identifiers
    # from a syntactic perspective, even though they can't be used as variable names in code
    data = {"stages": [{"name": "test", "data": "data"}], "save": {"class": "user.class", "for": "user.for_value", "if": "user.if_value"}}
    scenario = Scenario.model_validate(data)
    assert scenario.save["class"] == "user.class"
    assert scenario.save["for"] == "user.for_value"
    assert scenario.save["if"] == "user.if_value"


def test_scenario_save_valid_underscore_variable_names():
    data = {
        "stages": [{"name": "test", "data": "data"}],
        "save": {"_": "user.single_underscore", "__": "user.double_underscore", "___": "user.triple_underscore", "_private": "user.private", "__private__": "user.dunder_private"},
    }
    scenario = Scenario.model_validate(data)
    assert scenario.save["_"] == "user.single_underscore"
    assert scenario.save["__"] == "user.double_underscore"
    assert scenario.save["___"] == "user.triple_underscore"
    assert scenario.save["_private"] == "user.private"
    assert scenario.save["__private__"] == "user.dunder_private"


def test_scenario_save_valid_complex_jmespath_expressions():
    data = {
        "stages": [{"name": "test", "data": "data"}],
        "save": {
            "filtered_users": "users[?age > `18`]",
            "mapped_names": "users[*].name",
            "first_active": "users[?active][0]",
            "nested_access": "data.nested.deeply.nested.value",
            "pipe_expression": "users | [0]",
            "function_call": "length(users)",
            "conditional": "users[0] || `default`",
        },
    }
    scenario = Scenario.model_validate(data)
    assert scenario.save["filtered_users"] == "users[?age > `18`]"
    assert scenario.save["mapped_names"] == "users[*].name"
    assert scenario.save["first_active"] == "users[?active][0]"
    assert scenario.save["nested_access"] == "data.nested.deeply.nested.value"
    assert scenario.save["pipe_expression"] == "users | [0]"
    assert scenario.save["function_call"] == "length(users)"
    assert scenario.save["conditional"] == "users[0] || `default`"


def test_scenario_save_multiple_validation_errors():
    data = {
        "stages": [{"name": "test", "data": "data"}],
        "save": {
            "1invalid": "user.id",  # Invalid variable name
            "valid_name": "user.[invalid}",  # Invalid JMESPath
        },
    }
    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(data)
    # Should fail on the first validation error encountered
    assert "Key '1invalid' is not a valid Python variable name" in str(exc_info.value)
