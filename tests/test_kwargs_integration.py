from typing import Any

import pytest
from pydantic import ValidationError

from pytest_http.models import FunctionCall, Request, Response, Save, Stage, Verify


@pytest.mark.parametrize(
    "kwargs,expected_kwargs",
    [
        ({"param1": "value1", "param2": 42}, {"param1": "value1", "param2": 42}),
        (None, None),
    ],
)
def test_function_call_model(kwargs: dict[str, Any] | None, expected_kwargs: dict[str, Any] | None) -> None:
    func_call = FunctionCall(function="tests.test_module:test_function", kwargs=kwargs)
    assert func_call.function == "tests.test_module:test_function"
    assert func_call.kwargs == expected_kwargs


def test_verify_with_function_call() -> None:
    verify = Verify(
        status=200, functions=["tests.test_module:simple_function", FunctionCall(function="tests.test_module:function_with_kwargs", kwargs={"expected_status": 200, "timeout": 5.0})]
    )
    assert verify.status == 200
    assert len(verify.functions) == 2
    assert verify.functions[0] == "tests.test_module:simple_function"
    assert isinstance(verify.functions[1], FunctionCall)
    assert verify.functions[1].function == "tests.test_module:function_with_kwargs"
    assert verify.functions[1].kwargs == {"expected_status": 200, "timeout": 5.0}


def test_save_config_with_function_call() -> None:
    save_config = Save(
        vars={"test_var": "json.field"},
        functions=["tests.test_module:simple_function", FunctionCall(function="tests.test_module:function_with_kwargs", kwargs={"field_path": "data.items", "default_value": "unknown"})],
    )
    assert save_config.vars == {"test_var": "json.field"}
    assert len(save_config.functions) == 2
    assert save_config.functions[0] == "tests.test_module:simple_function"
    assert isinstance(save_config.functions[1], FunctionCall)
    assert save_config.functions[1].function == "tests.test_module:function_with_kwargs"
    assert save_config.functions[1].kwargs == {"field_path": "data.items", "default_value": "unknown"}


def test_stage_with_function_calls() -> None:
    stage = Stage(
        name="test_stage",
        request=Request(url="https://api.example.com/test"),  # No request fields in this stage
        response=Response(
            verify=Verify(functions=[FunctionCall(function="tests.test_module:verify_function", kwargs={"expected_text": "Hello", "case_sensitive": False})]),
            save=Save(functions=[FunctionCall(function="tests.test_module:save_function", kwargs={"extract_key": "user_id", "default_value": 0})]),
        ),
    )
    assert stage.name == "test_stage"
    assert stage.request.url == "https://api.example.com/test"
    assert stage.response.verify.functions[0].function == "tests.test_module:verify_function"
    assert stage.response.verify.functions[0].kwargs == {"expected_text": "Hello", "case_sensitive": False}
    assert stage.response.save.functions[0].function == "tests.test_module:save_function"
    assert stage.response.save.functions[0].kwargs == {"extract_key": "user_id", "default_value": 0}


@pytest.mark.parametrize(
    "scenario_data,expected_verify,expected_save",
    [
        (
            {
                "stages": [
                    {
                        "name": "test_stage",
                        "request": {"url": "https://api.example.com/test"},  # No request fields in this stage
                        "response": {
                            "verify": {"functions": [{"function": "tests.test_module:verify_function", "kwargs": {"expected_status": 200, "timeout": 5.0}}]},
                            "save": {"functions": [{"function": "tests.test_module:save_function", "kwargs": {"extract_key": "user_id", "default_value": 0}}]},
                        },
                    }
                ]
            },
            {"expected_status": 200, "timeout": 5.0},
            {"extract_key": "user_id", "default_value": 0},
        ),
        (
            {
                "stages": [
                    {
                        "name": "simple_stage",
                        "request": {"url": "https://api.example.com/test"},
                        "response": {"verify": {"status": 200}, "save": {"vars": {"user_id": "response.id"}}},
                    }
                ]
            },
            None,
            None,
        ),
    ],
)
def test_scenario_with_function_calls(scenario_data: dict[str, Any], expected_verify: dict[str, Any] | None, expected_save: dict[str, Any] | None) -> None:
    from pytest_http.models import Scenario

    scenario = Scenario.model_validate(scenario_data)
    assert len(scenario.stages) == 1

    if expected_verify:
        verify_func = scenario.stages[0].response.verify.functions[0]
        assert isinstance(verify_func, FunctionCall)
        assert verify_func.kwargs == expected_verify

    if expected_save:
        save_func = scenario.stages[0].response.save.functions[0]
        assert isinstance(save_func, FunctionCall)
        assert save_func.kwargs == expected_save


def test_mixed_function_formats() -> None:
    scenario_data = {
        "stages": [
            {
                "name": "test_stage",
                "request": {"url": "https://api.example.com/test"},  # Added required URL
                "response": {
                    "verify": {"functions": ["tests.test_module:simple_function", {"function": "tests.test_module:function_with_kwargs", "kwargs": {"param": "value"}}]},
                    "save": {"functions": ["tests.test_module:simple_save_function", {"function": "tests.test_module:save_with_kwargs", "kwargs": {"field": "data"}}]},
                },
            }
        ]
    }

    from pytest_http.models import Scenario

    scenario = Scenario.model_validate(scenario_data)
    assert len(scenario.stages) == 1

    # Check verify functions
    verify_functions = scenario.stages[0].response.verify.functions
    assert len(verify_functions) == 2
    assert verify_functions[0] == "tests.test_module:simple_function"
    assert isinstance(verify_functions[1], FunctionCall)
    assert verify_functions[1].function == "tests.test_module:function_with_kwargs"
    assert verify_functions[1].kwargs == {"param": "value"}

    # Check save functions
    save_functions = scenario.stages[0].response.save.functions
    assert len(save_functions) == 2
    assert save_functions[0] == "tests.test_module:simple_save_function"
    assert isinstance(save_functions[1], FunctionCall)
    assert save_functions[1].function == "tests.test_module:save_with_kwargs"
    assert save_functions[1].kwargs == {"field": "data"}


@pytest.mark.parametrize(
    "function_name,should_raise,expected_error",
    [
        ("tests.test_module:valid_function", False, None),
        ("invalid_function", True, "must use 'module:function' syntax"),
        (":function", True, "is missing module path"),
        ("module:", True, "is missing function name"),
    ],
)
def test_function_validation_with_kwargs(function_name: str, should_raise: bool, expected_error: str | None) -> None:
    from pytest_http.models import validate_python_function_name

    if should_raise:
        with pytest.raises(ValueError, match=expected_error):
            validate_python_function_name(function_name)
    else:
        result = validate_python_function_name(function_name)
        assert result == function_name
