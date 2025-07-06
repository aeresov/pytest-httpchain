import pytest
from pydantic import ValidationError

from pytest_http.models import TestSpec


def test_test_definition_empty():
    data = {}
    test_spec = TestSpec.model_validate(data)
    assert test_spec.fixtures == []
    assert test_spec.marks == []
    assert test_spec.stages == []


def test_test_definition_with_all_fields():
    data = {
        "fixtures": ["user_id", "api_key"],
        "marks": ["slow", "integration"],
        "stages": [{"name": "test_stage", "data": {"key": "value"}}, {"name": "another_stage", "data": "simple_data"}],
    }
    test_spec = TestSpec.model_validate(data)
    assert test_spec.fixtures == ["user_id", "api_key"]
    assert test_spec.marks == ["slow", "integration"]
    assert len(test_spec.stages) == 2
    assert test_spec.stages[0].name == "test_stage"
    assert test_spec.stages[1].name == "another_stage"


def test_test_definition_cross_field_validator_no_conflict():
    data = {
        "fixtures": ["user_id", "api_key"],
        "stages": [
            {"name": "test", "data": "data", "save": {"result": "user.id", "status": "response.status"}},
        ],
    }
    test_spec = TestSpec.model_validate(data)
    assert test_spec.fixtures == ["user_id", "api_key"]
    assert len(test_spec.stages) == 1
    assert test_spec.stages[0].save["result"] == "user.id"
    assert test_spec.stages[0].save["status"] == "response.status"


def test_test_definition_cross_field_validator_conflict():
    data = {
        "fixtures": ["user_id", "api_key"],
        "stages": [
            {"name": "test", "data": "data", "save": {"user_id": "user.id"}},
        ],
    }
    with pytest.raises(ValidationError) as exc_info:
        TestSpec.model_validate(data)
    assert "Variable name 'user_id' conflicts with fixture name" in str(exc_info.value)


def test_test_definition_cross_field_validator_multiple_conflicts():
    data = {
        "fixtures": ["user_id", "api_key"],
        "stages": [
            {"name": "test", "data": "data", "save": {"api_key": "app.key"}},
        ],
    }
    with pytest.raises(ValidationError) as exc_info:
        TestSpec.model_validate(data)
    assert "Variable name 'api_key' conflicts with fixture name" in str(exc_info.value)


def test_test_definition_cross_field_validator_no_fixtures():
    data = {
        "stages": [
            {"name": "test", "data": "data", "save": {"user_id": "user.id"}},
        ]
    }
    test_spec = TestSpec.model_validate(data)
    assert test_spec.fixtures == []
    assert len(test_spec.stages) == 1
    assert test_spec.stages[0].save["user_id"] == "user.id"


def test_test_definition_cross_field_validator_no_save_fields():
    data = {
        "fixtures": ["user_id", "api_key"],
        "stages": [
            {"name": "test", "data": "data"},
        ],
    }
    test_spec = TestSpec.model_validate(data)
    assert test_spec.fixtures == ["user_id", "api_key"]
    assert len(test_spec.stages) == 1
    assert test_spec.stages[0].save is None


def test_test_definition_cross_field_validator_empty_save_fields():
    data = {
        "fixtures": ["user_id", "api_key"],
        "stages": [
            {"name": "test", "data": "data", "save": {}},
        ],
    }
    test_spec = TestSpec.model_validate(data)
    assert test_spec.fixtures == ["user_id", "api_key"]
    assert len(test_spec.stages) == 1
    assert test_spec.stages[0].save == {}


def test_test_definition_cross_field_validator_mixed_stages():
    data = {
        "fixtures": ["user_id", "api_key"],
        "stages": [
            {"name": "test1", "data": "data1", "save": {"result": "user.id"}},
            {"name": "test2", "data": "data2"},
            {"name": "test3", "data": "data3", "save": {"status": "response.status"}},
        ],
    }
    test_spec = TestSpec.model_validate(data)
    assert test_spec.fixtures == ["user_id", "api_key"]
    assert len(test_spec.stages) == 3
    assert test_spec.stages[0].save["result"] == "user.id"
    assert test_spec.stages[1].save is None
    assert test_spec.stages[2].save["status"] == "response.status"


def test_test_definition_with_extra_fields():
    data = {"fixtures": ["user_id"], "marks": ["slow"], "stages": [{"name": "test", "data": "data"}], "extra_field": "ignored"}
    test_spec = TestSpec.model_validate(data)
    assert test_spec.fixtures == ["user_id"]
    assert test_spec.marks == ["slow"]
    assert len(test_spec.stages) == 1
    assert not hasattr(test_spec, "extra_field")
