import pytest
from pydantic import ValidationError

from pytest_http.models import Stage, Verify, validate_python_function_name


@pytest.mark.parametrize(
    "functions_input,expected_functions,description",
    [
        (["json:loads"], ["json:loads"], "single_function"),
        (["os:getcwd", "json:dumps"], ["os:getcwd", "json:dumps"], "multiple_functions"),
        (None, None, "none_functions"),
        ([], [], "empty_functions"),
    ],
)
def test_verify_model_functions_handling(functions_input, expected_functions, description):
    if description == "none_functions":
        verify = Verify()
    else:
        verify = Verify(functions=functions_input)
    assert verify.functions == expected_functions


@pytest.mark.parametrize(
    "invalid_function_name,expected_error",
    [
        ("invalid_function", "must use 'module:function' syntax"),
        ("module:", "is missing function name"),
        (":function", "is missing module path"),
        ("nonexistent_module:function", "Cannot import module"),
        ("json:nonexistent_function", "Function 'nonexistent_function' not found"),
        ("json:loads", None),  # Valid function
    ],
)
def test_verify_functions_validation(invalid_function_name, expected_error):
    if expected_error is None:
        # Should not raise an error
        result = validate_python_function_name(invalid_function_name)
        assert result == invalid_function_name
    else:
        with pytest.raises(ValueError, match=expected_error):
            validate_python_function_name(invalid_function_name)


@pytest.mark.parametrize(
    "stage_data,expected_functions",
    [
        ({"name": "test", "verify": {"functions": ["json:loads"]}}, ["json:loads"]),
        ({"name": "test", "verify": {"functions": ["os:getcwd", "json:dumps"]}}, ["os:getcwd", "json:dumps"]),
        ({"name": "test", "verify": {"functions": []}}, []),
        ({"name": "test", "verify": {}}, None),
        ({"name": "test"}, None),
    ],
)
def test_stage_verify_functions_handling(stage_data, expected_functions):
    stage = Stage.model_validate(stage_data)
    
    if expected_functions is not None:
        assert stage.verify is not None
        assert stage.verify.functions == expected_functions
    else:
        if stage.verify:
            assert stage.verify.functions is None


def test_verify_functions_with_status_and_json():
    stage_data = {
        "name": "test",
        "verify": {
            "status": 200,
            "json": {"json.some_field": "expected_value"},
            "functions": ["json:loads"]
        }
    }
    stage = Stage.model_validate(stage_data)
    
    assert stage.verify is not None
    assert stage.verify.status.value == 200
    assert stage.verify.json_data == {"json.some_field": "expected_value"}
    assert stage.verify.functions == ["json:loads"]


def test_verify_functions_optional_field():
    stage_data = {"name": "test_stage"}
    stage = Stage.model_validate(stage_data)
    assert stage.verify is None


def test_verify_functions_invalid_function_name():
    invalid_name = "invalid_function"
    data = {"name": "test", "verify": {"functions": [invalid_name]}}
    
    with pytest.raises(ValidationError):
        Stage.model_validate(data)


def test_verify_functions_valid_function_names():
    valid_names = ["json:loads", "os:getcwd"]
    data = {"name": "test", "verify": {"functions": valid_names}}
    stage = Stage.model_validate(data)
    
    assert stage.verify is not None
    assert stage.verify.functions == valid_names


@pytest.mark.parametrize(
    "functions_data",
    [
        None,           # No functions
        ["json:loads"], # Only functions
        [],             # Empty functions list
    ],
)
def test_verify_functions_optional_fields(functions_data):
    verify = Verify(functions=functions_data)
    
    if functions_data is None:
        assert verify.functions is None
    else:
        assert verify.functions == functions_data