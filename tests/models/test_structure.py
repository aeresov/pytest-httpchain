import pytest
from pydantic import ValidationError

from pytest_http.models import Scenario


def test_structure_with_fixtures_and_marks():
    data = {"fixtures": ["user_id", "api_key"], "marks": ["slow", "integration"], "stages": [{"name": "test", "data": "test_data"}]}

    test_spec = Scenario.model_validate(data)
    assert test_spec.fixtures == ["user_id", "api_key"]
    assert test_spec.marks == ["slow", "integration"]
    assert len(test_spec.stages) == 1


def test_structure_empty():
    data = {}

    test_spec = Scenario.model_validate(data)
    assert test_spec.fixtures == []
    assert test_spec.marks == []
    assert test_spec.stages == []


def test_structure_with_extra_fields():
    data = {"fixtures": ["user_id"], "marks": ["slow"], "stages": [{"name": "test", "data": "test_data"}], "extra_field": "ignored"}

    test_spec = Scenario.model_validate(data)
    assert test_spec.fixtures == ["user_id"]
    assert test_spec.marks == ["slow"]
    assert len(test_spec.stages) == 1


def test_structure_with_none_values():
    data = {"fixtures": None, "marks": None, "stages": None}

    test_spec = Scenario.model_validate(data)
    assert test_spec.fixtures == []
    assert test_spec.marks == []
    assert test_spec.stages == []


def test_structure_invalid_fixtures_type():
    data = {"fixtures": "not_a_list", "marks": ["slow"], "stages": [{"name": "test", "data": "test_data"}]}

    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(data)
    assert "Input should be a valid list" in str(exc_info.value)


def test_structure_invalid_marks_type():
    data = {"fixtures": ["user_id"], "marks": "not_a_list", "stages": [{"name": "test", "data": "test_data"}]}

    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(data)
    assert "Input should be a valid list" in str(exc_info.value)
