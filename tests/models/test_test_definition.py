import pytest
from pydantic import ValidationError

from pytest_http.models import TestDefinition


def test_test_definition_empty():
    data = {}
    test_def = TestDefinition.model_validate(data)
    assert test_def.fixtures == []
    assert test_def.marks == []
    assert test_def.stages == []


def test_test_definition_with_all_fields():
    data = {
        "fixtures": ["fixture1", "fixture2"],
        "marks": ["mark1", "mark2"],
        "stages": [
            {"name": "test_stage", "data": "test_data"}
        ]
    }
    test_def = TestDefinition.model_validate(data)
    assert test_def.fixtures == ["fixture1", "fixture2"]
    assert test_def.marks == ["mark1", "mark2"]
    assert len(test_def.stages) == 1
    assert test_def.stages[0].name == "test_stage"
    assert test_def.stages[0].data == "test_data"


def test_test_definition_cross_field_validator_no_conflict():
    data = {
        "fixtures": ["user_data", "config"],
        "stages": [
            {"name": "test", "data": "data", "save": {"result": "user.id", "status": "response.status"}},
        ],
    }
    
    test_def = TestDefinition.model_validate(data)
    assert test_def.fixtures == ["user_data", "config"]
    assert len(test_def.stages) == 1
    assert test_def.stages[0].save["result"] == "user.id"
    assert test_def.stages[0].save["status"] == "response.status"


def test_test_definition_cross_field_validator_conflict():
    data = {
        "fixtures": ["user_data", "config"],
        "stages": [
            {"name": "test", "data": "data", "save": {"user_data": "user.id"}},
        ],
    }
    
    with pytest.raises(ValidationError) as exc_info:
        TestDefinition.model_validate(data)
    assert "Variable name 'user_data' conflicts with fixture name" in str(exc_info.value)


def test_test_definition_cross_field_validator_multiple_conflicts():
    data = {
        "fixtures": ["user_data", "config", "auth_token"],
        "stages": [
            {"name": "test1", "data": "data1", "save": {"result": "user.id"}},
            {"name": "test2", "data": "data2", "save": {"config": "app.config", "user_data": "user.name"}},
        ],
    }
    
    with pytest.raises(ValidationError) as exc_info:
        TestDefinition.model_validate(data)
    error_msg = str(exc_info.value)
    assert "conflicts with fixture name" in error_msg
    assert "config" in error_msg or "user_data" in error_msg


def test_test_definition_cross_field_validator_no_fixtures():
    data = {
        "stages": [
            {"name": "test", "data": "data", "save": {"user_data": "user.id", "config": "app.config"}},
        ],
    }
    
    test_def = TestDefinition.model_validate(data)
    assert test_def.fixtures == []
    assert len(test_def.stages) == 1
    assert test_def.stages[0].save["user_data"] == "user.id"
    assert test_def.stages[0].save["config"] == "app.config"


def test_test_definition_cross_field_validator_no_save_fields():
    data = {
        "fixtures": ["user_data", "config"],
        "stages": [
            {"name": "test1", "data": "data1"},
            {"name": "test2", "data": "data2"},
        ],
    }
    
    test_def = TestDefinition.model_validate(data)
    assert test_def.fixtures == ["user_data", "config"]
    assert len(test_def.stages) == 2
    assert test_def.stages[0].save is None
    assert test_def.stages[1].save is None


def test_test_definition_cross_field_validator_empty_save_fields():
    data = {
        "fixtures": ["user_data", "config"],
        "stages": [
            {"name": "test1", "data": "data1", "save": {}},
            {"name": "test2", "data": "data2"},
        ],
    }
    
    test_def = TestDefinition.model_validate(data)
    assert test_def.fixtures == ["user_data", "config"]
    assert len(test_def.stages) == 2
    assert test_def.stages[0].save == {}
    assert test_def.stages[1].save is None


def test_test_definition_cross_field_validator_mixed_stages():
    data = {
        "fixtures": ["user_data", "config"],
        "stages": [
            {"name": "test1", "data": "data1", "save": {"result": "user.id"}},
            {"name": "test2", "data": "data2"},
            {"name": "test3", "data": "data3", "save": {"status": "response.status"}},
        ],
    }
    
    test_def = TestDefinition.model_validate(data)
    assert test_def.fixtures == ["user_data", "config"]
    assert len(test_def.stages) == 3
    assert test_def.stages[0].save["result"] == "user.id"
    assert test_def.stages[1].save is None
    assert test_def.stages[2].save["status"] == "response.status"


def test_test_definition_with_extra_fields():
    data = {
        "fixtures": ["fixture1"],
        "marks": ["mark1"],
        "stages": [{"name": "test", "data": "data"}],
        "unknown_field": "should_be_ignored"
    }
    test_def = TestDefinition.model_validate(data)
    assert test_def.fixtures == ["fixture1"]
    assert test_def.marks == ["mark1"]
    assert len(test_def.stages) == 1
    assert not hasattr(test_def, "unknown_field")