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


@pytest.mark.parametrize(
    "stage_names,expected_count",
    [
        (["string_stage", "number_stage", "dict_stage", "list_stage", "boolean_stage", "null_stage"], 6),
        (["single_stage"], 1),
        ([], 0),
    ],
)
def test_scenario_with_multiple_stages(stage_names, expected_count):
    data = {"stages": [{"name": name} for name in stage_names]}
    scenario = Scenario.model_validate(data)
    
    assert len(scenario.stages) == expected_count
    for i, expected_name in enumerate(stage_names):
        assert scenario.stages[i].name == expected_name


@pytest.mark.parametrize(
    "save_data,expected_vars",
    [
        ({"vars": {"result": "response.result", "status": "response.status"}}, {"result": "response.result", "status": "response.status"}),
        ({"vars": {"token": "response.token"}}, {"token": "response.token"}),
        ({"vars": {}}, {}),
    ],
)
def test_scenario_stages_with_save_field(save_data, expected_vars):
    data = {"stages": [{"name": "stage_with_save", "save": save_data}]}
    scenario = Scenario.model_validate(data)
    
    assert len(scenario.stages) == 1
    if expected_vars:
        assert scenario.stages[0].save.vars == expected_vars
    else:
        assert scenario.stages[0].save.vars == {}


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


@pytest.mark.parametrize(
    "fixtures,save_vars,expected_conflict",
    [
        (["user_id", "api_key"], {"vars": {"user_id": "user.id"}}, "user_id"),
        (["user_id", "api_key"], {"vars": {"api_key": "app.key"}}, "api_key"),
        (["user_id"], {"vars": {"user_id": "user.id", "api_key": "app.key"}}, "user_id"),
    ],
)
def test_scenario_fixture_variable_conflicts(fixtures, save_vars, expected_conflict):
    data = {"fixtures": fixtures, "stages": [{"name": "test", "save": save_vars}]}
    
    with pytest.raises(ValidationError) as exc_info:
        Scenario.model_validate(data)
    assert f"Variable name '{expected_conflict}' conflicts with fixture name" in str(exc_info.value)


@pytest.mark.parametrize(
    "data,description",
    [
        ({"stages": [{"name": "test", "save": {"vars": {"user_id": "user.id"}}}]}, "no_fixtures"),
        ({"fixtures": ["user_id", "api_key"], "stages": [{"name": "test"}]}, "no_save"),
        ({"fixtures": ["user_id", "api_key"], "stages": [{"name": "test", "save": {"vars": {}}}]}, "empty_save"),
    ],
)
def test_scenario_no_fixture_conflicts(data, description):
    scenario = Scenario.model_validate(data)
    assert len(scenario.stages) == 1


@pytest.mark.parametrize(
    "fixtures,stages_data,expected_saves",
    [
        (
            ["user_id", "api_key"],
            [
                {"name": "test1", "save": {"vars": {"result": "user.id"}}},
                {"name": "test2"},
                {"name": "test3", "save": {"vars": {"status": "response.status"}}},
            ],
            [{"result": "user.id"}, None, {"status": "response.status"}]
        ),
    ],
)
def test_scenario_mixed_stages_validation(fixtures, stages_data, expected_saves):
    data = {"fixtures": fixtures, "stages": stages_data}
    scenario = Scenario.model_validate(data)
    
    assert scenario.fixtures == fixtures
    assert len(scenario.stages) == len(stages_data)
    
    for i, expected_save in enumerate(expected_saves):
        if expected_save is None:
            assert scenario.stages[i].save is None
        else:
            assert scenario.stages[i].save.vars == expected_save


def test_scenario_complete_integration():
    data = {
        "fixtures": ["user_id", "api_key"],
        "marks": ["slow", "integration"],
        "stages": [
            {"name": "login", "save": {"vars": {"token": "response.token", "profile_id": "response.user.id"}}},
            {"name": "get_profile", "save": {"vars": {"profile": "response.profile"}}},
        ],
    }

    scenario = Scenario.model_validate(data)
    assert scenario.fixtures == ["user_id", "api_key"]
    assert scenario.marks == ["slow", "integration"]
    assert len(scenario.stages) == 2
    
    # Verify first stage
    assert scenario.stages[0].name == "login"
    assert scenario.stages[0].save.vars["token"] == "response.token"
    assert scenario.stages[0].save.vars["profile_id"] == "response.user.id"
    
    # Verify second stage
    assert scenario.stages[1].name == "get_profile"
    assert scenario.stages[1].save.vars["profile"] == "response.profile"
