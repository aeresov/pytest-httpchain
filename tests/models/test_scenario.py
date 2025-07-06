import pytest
from pydantic import ValidationError

from pytest_http.models import Scenario


@pytest.mark.parametrize(
    "data,expected_stages",
    [
        ({"stages": []}, []),
        ({"stages": None}, []),
        ({"stages": [{"name": "stage1", "data": "test_data1"}, {"name": "stage2", "data": "test_data2"}]}, 2),
    ],
)
def test_scenario_stages_handling(data, expected_stages):
    scenario = Scenario.model_validate(data)
    if isinstance(expected_stages, int):
        assert len(scenario.stages) == expected_stages
        assert scenario.stages[0].name == "stage1"
        assert scenario.stages[0].data == "test_data1"
        assert scenario.stages[1].name == "stage2"
        assert scenario.stages[1].data == "test_data2"
    else:
        assert scenario.stages == expected_stages


@pytest.mark.parametrize(
    "data,expected_fixtures,expected_marks",
    [
        ({}, [], []),
        ({"fixtures": None, "marks": None}, [], []),
        ({"fixtures": ["user_id", "api_key"], "marks": ["slow", "integration"]}, ["user_id", "api_key"], ["slow", "integration"]),
    ],
)
def test_scenario_fixtures_and_marks(data, expected_fixtures, expected_marks):
    scenario = Scenario.model_validate(data)
    assert scenario.fixtures == expected_fixtures
    assert scenario.marks == expected_marks


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

    scenario = Scenario.model_validate(data)
    assert len(scenario.stages) == 6
    assert scenario.stages[0].data == "simple_string"
    assert scenario.stages[1].data == 42
    assert scenario.stages[2].data == {"key": "value", "nested": {"inner": "data"}}
    assert scenario.stages[3].data == ["item1", "item2", {"nested": "object"}]
    assert scenario.stages[4].data is True
    assert scenario.stages[5].data is None


def test_scenario_with_stages_containing_save_field():
    data = {"stages": [{"name": "stage_with_save", "data": {"test": "data"}, "save": {"result": "response.result", "status": "response.status"}}]}

    scenario = Scenario.model_validate(data)
    assert len(scenario.stages) == 1
    assert scenario.stages[0].save == {"result": "response.result", "status": "response.status"}


@pytest.mark.parametrize(
    "data,expected_error",
    [
        ({"stages": "not_a_list"}, "Input should be a valid list"),
        ({"fixtures": "not_a_list"}, "Input should be a valid list"),
        ({"marks": "not_a_list"}, "Input should be a valid list"),
        ({"stages": [{"name": "stage1", "data": "test_data"}, {"invalid": "structure"}]}, "Field required"),
    ],
)
def test_scenario_validation_errors(data, expected_error):
    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(data)
    assert expected_error in str(exc_info.value)


def test_scenario_ignores_extra_fields():
    data = {"stages": [{"name": "stage1", "data": "test_data"}], "fixtures": ["user_id"], "marks": ["slow"], "extra_field": "should_be_ignored"}

    scenario = Scenario.model_validate(data)
    assert len(scenario.stages) == 1
    assert scenario.fixtures == ["user_id"]
    assert scenario.marks == ["slow"]
    assert not hasattr(scenario, "extra_field")


def test_scenario_cross_field_validator_no_conflict():
    data = {
        "fixtures": ["user_id", "api_key"],
        "stages": [
            {"name": "test", "data": "data", "save": {"result": "user.id", "status": "response.status"}},
        ],
    }
    scenario = Scenario.model_validate(data)
    assert scenario.fixtures == ["user_id", "api_key"]
    assert len(scenario.stages) == 1
    assert scenario.stages[0].save["result"] == "user.id"
    assert scenario.stages[0].save["status"] == "response.status"


@pytest.mark.parametrize(
    "fixtures,save_vars,expected_conflict",
    [
        (["user_id", "api_key"], {"user_id": "user.id"}, "user_id"),
        (["user_id", "api_key"], {"api_key": "app.key"}, "api_key"),
        (["user_id"], {"user_id": "user.id", "api_key": "app.key"}, "user_id"),
    ],
)
def test_scenario_cross_field_validator_conflicts(fixtures, save_vars, expected_conflict):
    data = {
        "fixtures": fixtures,
        "stages": [
            {"name": "test", "data": "data", "save": save_vars},
        ],
    }
    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(data)
    assert f"Variable name '{expected_conflict}' conflicts with fixture name" in str(exc_info.value)


@pytest.mark.parametrize(
    "data",
    [
        {"stages": [{"name": "test", "data": "data", "save": {"user_id": "user.id"}}]},
        {"fixtures": ["user_id", "api_key"], "stages": [{"name": "test", "data": "data"}]},
        {"fixtures": ["user_id", "api_key"], "stages": [{"name": "test", "data": "data", "save": {}}]},
    ],
)
def test_scenario_cross_field_validator_no_validation_needed(data):
    scenario = Scenario.model_validate(data)
    assert len(scenario.stages) == 1


def test_scenario_cross_field_validator_mixed_stages():
    data = {
        "fixtures": ["user_id", "api_key"],
        "stages": [
            {"name": "test1", "data": "data1", "save": {"result": "user.id"}},
            {"name": "test2", "data": "data2"},
            {"name": "test3", "data": "data3", "save": {"status": "response.status"}},
        ],
    }
    scenario = Scenario.model_validate(data)
    assert scenario.fixtures == ["user_id", "api_key"]
    assert len(scenario.stages) == 3
    assert scenario.stages[0].save["result"] == "user.id"
    assert scenario.stages[1].save is None
    assert scenario.stages[2].save["status"] == "response.status"


def test_scenario_complete_integration():
    data = {
        "fixtures": ["user_id", "api_key"],
        "marks": ["slow", "integration"],
        "stages": [
            {"name": "login", "data": {"username": "test", "password": "secret"}, "save": {"token": "response.token", "profile_id": "response.user.id"}},
            {"name": "get_profile", "data": {"user_id": "$user_id"}, "save": {"profile": "response.profile"}},
        ],
    }

    scenario = Scenario.model_validate(data)
    assert scenario.fixtures == ["user_id", "api_key"]
    assert scenario.marks == ["slow", "integration"]
    assert len(scenario.stages) == 2
    assert scenario.stages[0].name == "login"
    assert scenario.stages[0].save == {"token": "response.token", "profile_id": "response.user.id"}
    assert scenario.stages[1].name == "get_profile"
    assert scenario.stages[1].save == {"profile": "response.profile"}
