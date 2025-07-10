from typing import Any
from unittest.mock import Mock, patch

import pytest

from pytest_http.models import FunctionCall, SaveConfig, Scenario, Stage, Verify, Response
from pytest_http.pytest_plugin import call_function_with_kwargs, substitute_kwargs_variables


@pytest.fixture
def mock_response() -> Mock:
    response = Mock()
    response.status_code = 200
    response.text = "Hello World"
    response.headers = {"content-type": "application/json"}
    return response


@pytest.fixture
def sample_variables() -> dict[str, Any]:
    return {
        "status": 200,
        "text": "Hello World",
        "field1": "slideshow.title",
        "field2": "slideshow.author",
        "default": "unknown",
        "timeout": 5.0
    }


@pytest.fixture
def sample_kwargs() -> dict[str, Any]:
    return {
        "expected_status": "$status",
        "expected_text": "$text",
        "case_sensitive": True
    }


@pytest.mark.parametrize("kwargs,expected_kwargs", [
    ({"param1": "value1", "param2": 42}, {"param1": "value1", "param2": 42}),
    (None, None),
])
def test_function_call_model(kwargs: dict[str, Any] | None, expected_kwargs: dict[str, Any] | None) -> None:
    func_call = FunctionCall(
        function="test_module:test_function",
        kwargs=kwargs
    )

    assert func_call.function == "test_module:test_function"
    assert func_call.kwargs == expected_kwargs


def test_verify_with_function_call() -> None:
    verify = Verify(
        status=200,
        functions=[
            "test_module:simple_function",
            FunctionCall(
                function="test_module:function_with_kwargs",
                kwargs={"expected_status": 200, "timeout": 5.0}
            )
        ]
    )

    assert len(verify.functions) == 2
    assert verify.functions[0] == "test_module:simple_function"
    assert isinstance(verify.functions[1], FunctionCall)
    assert verify.functions[1].function == "test_module:function_with_kwargs"
    assert verify.functions[1].kwargs == {"expected_status": 200, "timeout": 5.0}


def test_save_config_with_function_call() -> None:
    save_config = SaveConfig(
        vars={"test_var": "json.field"},
        functions=[
            "test_module:simple_function",
            FunctionCall(
                function="test_module:function_with_kwargs",
                kwargs={"field_path": "data.items", "default_value": "unknown"}
            )
        ]
    )

    assert len(save_config.functions) == 2
    assert save_config.functions[0] == "test_module:simple_function"
    assert isinstance(save_config.functions[1], FunctionCall)
    assert save_config.functions[1].function == "test_module:function_with_kwargs"
    assert save_config.functions[1].kwargs == {"field_path": "data.items", "default_value": "unknown"}


def test_stage_with_function_calls() -> None:
    stage = Stage(
        name="test_stage",
        request={},  # No request fields in this stage
        response=Response(
            verify=Verify(
                functions=[
                    FunctionCall(
                        function="test_module:verify_function",
                        kwargs={"expected_text": "Hello", "case_sensitive": False}
                    )
                ]
            ),
            save=SaveConfig(
                functions=[
                    FunctionCall(
                        function="test_module:save_function",
                        kwargs={"extract_fields": ["title", "author"]}
                    )
                ]
            )
        )
    )

    assert stage.response.verify.functions[0].function == "test_module:verify_function"
    assert stage.response.verify.functions[0].kwargs == {"expected_text": "Hello", "case_sensitive": False}
    assert stage.response.save.functions[0].function == "test_module:save_function"
    assert stage.response.save.functions[0].kwargs == {"extract_fields": ["title", "author"]}


@pytest.mark.parametrize("scenario_data,expected_verify,expected_save", [
    (
        {
            "stages": [
                {
                    "name": "test_stage",
                    "request": {},  # No request fields in this stage
                    "response": {
                        "verify": {
                            "functions": [
                                {
                                    "function": "test_module:verify_function",
                                    "kwargs": {"expected_status": 200, "timeout": 5.0}
                                }
                            ]
                        },
                        "save": {
                            "functions": [
                                {
                                    "function": "test_module:save_function",
                                    "kwargs": {"field_path": "data.items", "default_value": "unknown"}
                                }
                            ]
                        }
                    }
                }
            ]
        },
        {"expected_status": 200, "timeout": 5.0},
        {"field_path": "data.items", "default_value": "unknown"}
    ),
    (
        {
            "stages": [
                {
                    "name": "test_stage",
                    "request": {},  # No request fields in this stage
                    "response": {
                        "verify": {"functions": ["test_module:simple_function"]},
                        "save": {"functions": ["test_module:save_function"]}
                    }
                }
            ]
        },
        None,
        None
    ),
])
def test_scenario_with_function_calls(scenario_data: dict[str, Any], expected_verify: dict[str, Any] | None, expected_save: dict[str, Any] | None) -> None:
    scenario = Scenario.model_validate(scenario_data)

    assert len(scenario.stages) == 1
    stage = scenario.stages[0]

    if expected_verify:
        verify_func = stage.response.verify.functions[0]
        assert isinstance(verify_func, FunctionCall)
        assert verify_func.function == "test_module:verify_function"
        assert verify_func.kwargs == expected_verify

        save_func = stage.response.save.functions[0]
        assert isinstance(save_func, FunctionCall)
        assert save_func.function == "test_module:save_function"
        assert save_func.kwargs == expected_save
    else:
        assert stage.response.verify.functions[0] == "test_module:simple_function"
        assert stage.response.save.functions[0] == "test_module:save_function"


def test_mixed_function_formats() -> None:
    scenario_data = {
        "stages": [
            {
                "name": "test_stage",
                "request": {},  # No request fields in this stage
                "response": {
                    "verify": {
                        "functions": [
                            "test_module:simple_function",
                            {"function": "test_module:function_with_kwargs", "kwargs": {"param": "value"}}
                        ]
                    },
                    "save": {
                        "functions": [
                            "test_module:simple_save_function",
                            {"function": "test_module:save_with_kwargs", "kwargs": {"field": "data"}}
                        ]
                    }
                }
            }
        ]
    }

    scenario = Scenario.model_validate(scenario_data)
    stage = scenario.stages[0]

    verify_functions = stage.response.verify.functions
    assert verify_functions[0] == "test_module:simple_function"
    assert isinstance(verify_functions[1], FunctionCall)
    assert verify_functions[1].function == "test_module:function_with_kwargs"
    assert verify_functions[1].kwargs == {"param": "value"}

    save_functions = stage.response.save.functions
    assert save_functions[0] == "test_module:simple_save_function"
    assert isinstance(save_functions[1], FunctionCall)
    assert save_functions[1].function == "test_module:save_with_kwargs"
    assert save_functions[1].kwargs == {"field": "data"}


@pytest.mark.parametrize("kwargs,variables,expected", [
    (
        {"expected_status": "$status", "expected_text": "$text", "case_sensitive": True},
        {"status": 200, "text": "Hello World"},
        {"expected_status": 200, "expected_text": "Hello World", "case_sensitive": True}
    ),
    (
        {"param1": "value1", "param2": 42, "param3": True},
        {"unused": "value"},
        {"param1": "value1", "param2": 42, "param3": True}
    ),
    (
        {"string_param": "$string_var", "number_param": "$number_var", "boolean_param": "$boolean_var"},
        {"string_var": "test string", "number_var": 42, "boolean_var": True},
        {"string_param": "test string", "number_param": 42, "boolean_param": True}
    ),
    (
        {"expected_text": '"$text_var"', "field_path": '"$path_var"'},
        {"text_var": "Hello World", "path_var": "slideshow.title"},
        {"expected_text": "Hello World", "field_path": "slideshow.title"}
    ),
    (
        {"param1": "$var1", "param2": "static_value", "param3": "$var2", "param4": 42},
        {"var1": "substituted_value", "var2": "another_value"},
        {"param1": "substituted_value", "param2": "static_value", "param3": "another_value", "param4": 42}
    ),
    (
        {"param1": "$var1", "param2": "static_value"},
        {},
        {"param1": "$var1", "param2": "static_value"}
    ),
])
def test_substitute_kwargs_variables(kwargs: dict[str, Any], variables: dict[str, Any], expected: dict[str, Any]) -> None:
    result = substitute_kwargs_variables(kwargs, variables)
    assert result == expected


def test_substitute_kwargs_variables_none() -> None:
    result = substitute_kwargs_variables(None, {"var": "value"})
    assert result is None


def test_substitute_kwargs_variables_nested() -> None:
    kwargs = {
        "fields": ["$field1", "$field2", "static_field"],
        "config": {"default_value": "$default", "timeout": "$timeout"}
    }

    variables = {
        "field1": "slideshow.title",
        "field2": "slideshow.author",
        "default": "unknown",
        "timeout": 5.0
    }

    result = substitute_kwargs_variables(kwargs, variables)

    expected = {
        "fields": ["slideshow.title", "slideshow.author", "static_field"],
        "config": {"default_value": "unknown", "timeout": 5.0}
    }

    assert result == expected


def test_substitute_kwargs_variables_complex_json() -> None:
    kwargs = {
        "config": {
            "filters": [
                {"field": "$field1", "value": "$value1"},
                {"field": "$field2", "value": "$value2"}
            ],
            "options": {"timeout": "$timeout", "retries": 3}
        }
    }

    variables = {
        "field1": "title",
        "value1": "Sample",
        "field2": "author",
        "value2": "Yours Truly",
        "timeout": 5.0
    }

    result = substitute_kwargs_variables(kwargs, variables)

    expected = {
        "config": {
            "filters": [
                {"field": "title", "value": "Sample"},
                {"field": "author", "value": "Yours Truly"}
            ],
            "options": {"timeout": 5.0, "retries": 3}
        }
    }

    assert result == expected


def test_call_function_with_kwargs(mock_response: Mock) -> None:
    def test_function(response: Mock, expected_status: int = 200, expected_text: str = "") -> bool:
        return (response.status_code == expected_status and
                expected_text in response.text)

    with patch('importlib.import_module') as mock_import:
        mock_module = Mock()
        mock_module.test_function = test_function
        mock_import.return_value = mock_module

        result = call_function_with_kwargs(
            "test_module:test_function",
            mock_response,
            {"expected_status": 200, "expected_text": "Hello"}
        )

        assert result is True

    with patch('importlib.import_module') as mock_import:
        mock_module = Mock()
        mock_module.test_function = test_function
        mock_import.return_value = mock_module

        result = call_function_with_kwargs(
            "test_module:test_function",
            mock_response
        )

        assert result is True


@pytest.mark.parametrize("error_type,expected_message", [
    (ImportError("Module not found"), "Error executing function 'nonexistent:function'"),
    (ValueError("Function failed"), "Error executing function 'test_module:failing_function'"),
])
def test_call_function_with_kwargs_error_handling(mock_response: Mock, error_type: Exception, expected_message: str) -> None:
    with patch('importlib.import_module') as mock_import:
        if isinstance(error_type, ImportError):
            mock_import.side_effect = error_type
            with pytest.raises(Exception) as exc_info:
                call_function_with_kwargs("nonexistent:function", mock_response)
        else:
            def failing_function(response: Mock, **kwargs: Any) -> None:
                raise error_type

            mock_module = Mock()
            mock_module.failing_function = failing_function
            mock_import.return_value = mock_module

            with pytest.raises(Exception) as exc_info:
                call_function_with_kwargs("test_module:failing_function", mock_response)

        assert expected_message in str(exc_info.value)


@pytest.mark.parametrize("function_name,should_raise,expected_error", [
    ("test_module:valid_function", False, None),
    ("invalid_function", True, "must use 'module:function' syntax"),
    (":function", True, "is missing module path"),
    ("module:", True, "is missing function name"),
])
def test_function_validation_with_kwargs(function_name: str, should_raise: bool, expected_error: str | None) -> None:
    from pytest_http.models import validate_python_function_name

    if should_raise:
        with pytest.raises(ValueError, match=expected_error):
            validate_python_function_name(function_name)
    else:
        result = validate_python_function_name(function_name)
        assert result == function_name
