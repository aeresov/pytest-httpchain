import pytest
from pydantic import ValidationError

from pytest_http.models import TestSpec


def test_scenario_with_stages():
    data = {"stages": [{"name": "stage1", "data": "test_data1"}, {"name": "stage2", "data": "test_data2"}]}

    test_spec = TestSpec.model_validate(data)
    assert len(test_spec.stages) == 2
    assert test_spec.stages[0].name == "stage1"
    assert test_spec.stages[0].data == "test_data1"
    assert test_spec.stages[1].name == "stage2"
    assert test_spec.stages[1].data == "test_data2"


@pytest.mark.parametrize(
    "data,expected_stages",
    [
        ({"stages": []}, []),
        ({"stages": None}, []),
    ],
)
def test_scenario_empty_stages(data, expected_stages):
    test_spec = TestSpec.model_validate(data)
    assert test_spec.stages == expected_stages


def test_scenario_with_extra_fields():
    data = {"stages": [{"name": "stage1", "data": "test_data"}], "extra_field": "should_be_ignored"}

    test_spec = TestSpec.model_validate(data)
    assert len(test_spec.stages) == 1
    assert test_spec.stages[0].name == "stage1"
    assert not hasattr(test_spec, "extra_field")


def test_scenario_invalid_stages_type():
    data = {"stages": "not_a_list"}

    with pytest.raises(ValidationError) as exc_info:
        TestSpec.model_validate(data)
    assert "Input should be a valid list" in str(exc_info.value)


def test_scenario_invalid_stage_structure():
    data = {"stages": [{"name": "stage1", "data": "test_data"}, {"invalid": "structure"}]}

    with pytest.raises(ValidationError) as exc_info:
        TestSpec.model_validate(data)
    assert "Field required" in str(exc_info.value)


def test_scenario_with_complex_stages():
    data = {
        "stages": [
            {"name": "string_stage", "data": "simple_string"},
            {"name": "number_stage", "data": 42},
            {"name": "dict_stage", "data": {"key": "value", "nested": {"inner": "data"}}},
            {"name": "list_stage", "data": ["item1", "item2", {"nested": "object"}]},
            {"name": "boolean_stage", "data": True},
            {"name": "null_stage", "data": None},
        ]
    }

    test_spec = TestSpec.model_validate(data)
    assert len(test_spec.stages) == 6
    assert test_spec.stages[0].data == "simple_string"
    assert test_spec.stages[1].data == 42
    assert test_spec.stages[2].data == {"key": "value", "nested": {"inner": "data"}}
    assert test_spec.stages[3].data == ["item1", "item2", {"nested": "object"}]
    assert test_spec.stages[4].data is True
    assert test_spec.stages[5].data is None


def test_scenario_with_stages_containing_save_field():
    data = {"stages": [{"name": "stage_with_save", "data": {"test": "data"}, "save": {"result": "response.result", "status": "response.status"}}]}

    test_spec = TestSpec.model_validate(data)
    assert len(test_spec.stages) == 1
    assert test_spec.stages[0].save == {"result": "response.result", "status": "response.status"}
