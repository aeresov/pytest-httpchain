import pytest
from pydantic import ValidationError

from pytest_http.models import Scenario, SaveConfig


@pytest.mark.parametrize(
    "data,expected_stages",
    [
        ({"stages": []}, []),
        ({"stages": None}, []),
        ({"stages": [{"name": "stage1"}, {"name": "stage2"}]}, 2),
    ],
)
def test_scenario_stages_handling(data, expected_stages):
    scenario = Scenario.model_validate(data)
    if isinstance(expected_stages, int):
        assert len(scenario.stages) == expected_stages
        assert scenario.stages[0].name == "stage1"
        assert scenario.stages[1].name == "stage2"
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
            {"name": "string_stage"},
            {"name": "number_stage"},
            {"name": "dict_stage"},
            {"name": "list_stage"},
            {"name": "boolean_stage"},
            {"name": "null_stage"},
        ]
    }

    scenario = Scenario.model_validate(data)
    assert len(scenario.stages) == 6
    assert scenario.stages[0].name == "string_stage"
    assert scenario.stages[1].name == "number_stage"
    assert scenario.stages[2].name == "dict_stage"
    assert scenario.stages[3].name == "list_stage"
    assert scenario.stages[4].name == "boolean_stage"
    assert scenario.stages[5].name == "null_stage"


def test_scenario_with_stages_containing_save_field():
    data = {"stages": [{"name": "stage_with_save", "save": {"result": "response.result", "status": "response.status"}}]}

    scenario = Scenario.model_validate(data)
    assert len(scenario.stages) == 1
    assert isinstance(scenario.stages[0].save, SaveConfig)
    assert scenario.stages[0].save.vars["result"] == "response.result"
    assert scenario.stages[0].save.vars["status"] == "response.status"


@pytest.mark.parametrize(
    "data,expected_error",
    [
        ({"stages": "not_a_list"}, "Input should be a valid list"),
        ({"fixtures": "not_a_list"}, "Input should be a valid list"),
        ({"marks": "not_a_list"}, "Input should be a valid list"),
        ({"stages": [{"name": "stage1"}, {"invalid": "structure"}]}, "Field required"),
    ],
)
def test_scenario_validation_errors(data, expected_error):
    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(data)
    assert expected_error in str(exc_info.value)


def test_scenario_ignores_extra_fields():
    data = {"stages": [{"name": "stage1"}], "fixtures": ["user_id"], "marks": ["slow"], "extra_field": "should_be_ignored"}

    scenario = Scenario.model_validate(data)
    assert len(scenario.stages) == 1
    assert scenario.fixtures == ["user_id"]
    assert scenario.marks == ["slow"]
    assert not hasattr(scenario, "extra_field")


def test_scenario_cross_field_validator_no_conflict():
    data = {
        "fixtures": ["user_id", "api_key"],
        "stages": [
            {"name": "test", "save": {"result": "user.id", "status": "response.status"}},
        ],
    }
    scenario = Scenario.model_validate(data)
    assert scenario.fixtures == ["user_id", "api_key"]
    assert len(scenario.stages) == 1
    assert isinstance(scenario.stages[0].save, SaveConfig)
    assert scenario.stages[0].save.vars["result"] == "user.id"
    assert scenario.stages[0].save.vars["status"] == "response.status"


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
            {"name": "test", "save": save_vars},
        ],
    }
    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(data)
    assert f"Variable name '{expected_conflict}' conflicts with fixture name" in str(exc_info.value)


@pytest.mark.parametrize(
    "data",
    [
        {"stages": [{"name": "test", "save": {"user_id": "user.id"}}]},
        {"fixtures": ["user_id", "api_key"], "stages": [{"name": "test"}]},
        {"fixtures": ["user_id", "api_key"], "stages": [{"name": "test", "save": {}}]},
    ],
)
def test_scenario_cross_field_validator_no_validation_needed(data):
    scenario = Scenario.model_validate(data)
    assert len(scenario.stages) == 1


def test_scenario_cross_field_validator_mixed_stages():
    data = {
        "fixtures": ["user_id", "api_key"],
        "stages": [
            {"name": "test1", "save": {"result": "user.id"}},
            {"name": "test2"},
            {"name": "test3", "save": {"status": "response.status"}},
        ],
    }
    scenario = Scenario.model_validate(data)
    assert scenario.fixtures == ["user_id", "api_key"]
    assert len(scenario.stages) == 3
    assert isinstance(scenario.stages[0].save, SaveConfig)
    assert scenario.stages[0].save.vars["result"] == "user.id"
    assert scenario.stages[1].save is None
    assert isinstance(scenario.stages[2].save, SaveConfig)
    assert scenario.stages[2].save.vars["status"] == "response.status"


def test_scenario_complete_integration():
    data = {
        "fixtures": ["user_id", "api_key"],
        "marks": ["slow", "integration"],
        "stages": [
            {"name": "login", "save": {"token": "response.token", "profile_id": "response.user.id"}},
            {"name": "get_profile", "save": {"profile": "response.profile"}},
        ],
    }

    scenario = Scenario.model_validate(data)
    assert scenario.fixtures == ["user_id", "api_key"]
    assert scenario.marks == ["slow", "integration"]
    assert len(scenario.stages) == 2
    assert scenario.stages[0].name == "login"
    assert isinstance(scenario.stages[0].save, SaveConfig)
    assert scenario.stages[0].save.vars["token"] == "response.token"
    assert scenario.stages[0].save.vars["profile_id"] == "response.user.id"
    assert scenario.stages[1].name == "get_profile"
    assert isinstance(scenario.stages[1].save, SaveConfig)
    assert scenario.stages[1].save.vars["profile"] == "response.profile"
