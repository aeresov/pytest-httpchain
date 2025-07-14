from http import HTTPMethod

import pytest
from pydantic import ValidationError

from pytest_http.models import (
    FunctionCall,
    Functions,
    Request,
    Response,
    Save,
    Scenario,
    Stage,
    Stages,
)


def test_function_call_valid_with_kwargs():
    func_call = FunctionCall(function="json:dumps", kwargs={"arg": "value"})
    assert func_call.function == "json:dumps"
    assert func_call.kwargs == {"arg": "value"}


def test_function_call_valid_without_kwargs():
    func_call = FunctionCall(function="json:dumps")
    assert func_call.function == "json:dumps"
    assert func_call.kwargs is None


def test_function_call_missing_required_function():
    with pytest.raises(ValidationError):
        FunctionCall(kwargs={"arg": "value"})


def test_function_call_invalid_function_format():
    with pytest.raises(ValidationError):
        FunctionCall(function="invalid_function_name")


def test_function_call_nonexistent_function():
    with pytest.raises(ValidationError):
        FunctionCall(function="json:nonexistent_function")


def test_functions_valid_mixed_types():
    functions = Functions(["json:dumps", FunctionCall(function="json:loads")])
    assert len(functions.root) == 2
    assert functions.root[0] == "json:dumps"
    assert isinstance(functions.root[1], FunctionCall)


def test_request_missing_required_url():
    with pytest.raises(ValidationError):
        Request(method=HTTPMethod.GET)


def test_request_invalid_http_method():
    with pytest.raises(ValidationError):
        Request(url="https://api.example.com", method="INVALID")  # type: ignore


def test_stage_missing_required_name():
    with pytest.raises(ValidationError):
        Stage(request=Request(url="https://api.example.com"))


def test_stage_missing_required_request():
    with pytest.raises(ValidationError):
        Stage(name="test_stage")


def test_scenario_validator_fixture_variable_conflict():
    with pytest.raises(ValidationError):
        Scenario(
            fixtures=["fixture1", "fixture2"],
            flow=Stages(
                root=[
                    Stage(
                        name="stage1",
                        request=Request(url="https://api.example.com"),
                        response=Response(
                            save=Save(vars={"fixture1": "response.token"}),
                        ),
                    )
                ]
            ),
        )


@pytest.mark.parametrize(
    "json_data",
    [
        {"key": "value"},
        [1, 2, 3],
        "string",
        123,
        True,
        None,
    ],
)
def test_request_json_serializable_validation(json_data):
    request = Request(url="https://api.example.com", json=json_data)
    assert request.json == json_data


def test_scenario_extra_fields_ignored():
    scenario = Scenario(
        fixtures=["fixture1"],
        extra_field="should_be_ignored",  # type: ignore
    )
    assert scenario.fixtures == ["fixture1"]
    assert not hasattr(scenario, "extra_field")


def test_fixtures_list():
    scenario = Scenario(fixtures=["fixture1"])
    assert scenario.fixtures == ["fixture1"]
    with pytest.raises(ValidationError):
        Scenario(fixtures=["fixture1", {"invalid": "fixture"}])
