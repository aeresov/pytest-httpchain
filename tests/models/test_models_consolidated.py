from http import HTTPMethod, HTTPStatus

import pytest
from pydantic import ValidationError

from pytest_http.models import SaveConfig, Stage, Verify, validate_jmespath_expression, validate_python_function_name, validate_python_variable_name


@pytest.mark.parametrize(
    "name,expected_name",
    [
        ("test_stage", "test_stage"),
        ("numeric_stage", "numeric_stage"),
        ("dict_stage", "dict_stage"),
        ("list_stage", "list_stage"),
        ("bool_stage", "bool_stage"),
        ("null_stage", "null_stage"),
    ],
)
def test_stage_with_different_data_types(name: str, expected_name: str):
    data_dict = {"name": name}
    stage = Stage.model_validate(data_dict)
    assert stage.name == expected_name


@pytest.mark.parametrize(
    "data,expected_error",
    [
        ({}, "name"),
    ],
)
def test_stage_missing_required_fields(data, expected_error):
    with pytest.raises(ValidationError, match=expected_error):
        Stage.model_validate(data)


def test_stage_empty_name():
    data = {"name": ""}
    stage = Stage.model_validate(data)
    assert stage.name == ""


@pytest.mark.parametrize(
    "save_data,expected_vars,expected_functions,description",
    [
        # Format with vars only
        (
            {"vars": {"user_id": "user.id", "user_name": "user.name"}},
            {"user_id": "user.id", "user_name": "user.name"},
            None,
            "vars_only"
        ),
        # Format with functions only
        (
            {"functions": ["json:loads", "os:getcwd"]},
            None,
            ["json:loads", "os:getcwd"],
            "functions_only"
        ),
        # Format with both vars and functions
        (
            {
                "vars": {"user_id": "user.id"},
                "functions": ["json:dumps"]
            },
            {"user_id": "user.id"},
            ["json:dumps"],
            "both_vars_and_functions"
        ),
    ],
)
def test_stage_save_formats(save_data, expected_vars, expected_functions, description):
    data = {"name": "test", "request": {"url": "https://api.example.com/test"}, "response": {"save": save_data}}
    stage = Stage.model_validate(data)

    assert isinstance(stage.response.save, SaveConfig)
    assert stage.response.save.vars == expected_vars
    assert stage.response.save.functions == expected_functions


@pytest.mark.parametrize(
    "save_value,expected_result,description",
    [
        (None, None, "with_none"),
        ({"vars": {}}, SaveConfig(vars={}), "with_empty_vars"),
        ("no_save", None, "without_save_field"),
    ],
)
def test_stage_save_optional_states(save_value, expected_result, description):
    if description == "without_save_field":
        data = {"name": "test", "request": {"url": "https://api.example.com/test"}}
    else:
        data = {"name": "test", "request": {"url": "https://api.example.com/test"}, "response": {"save": save_value}}

    stage = Stage.model_validate(data)

    if expected_result is None:
        assert stage.response.save is None
    else:
        assert isinstance(stage.response.save, SaveConfig)
        assert stage.response.save.vars == expected_result.vars
        assert stage.response.save.functions == expected_result.functions


@pytest.mark.parametrize(
    "invalid_name,field_type,expected_error",
    [
        # Variable name validation
        ("1invalid", "var", "'1invalid' is not a valid Python variable name"),
        ("user-id", "var", "'user-id' is not a valid Python variable name"),
        ("user id", "var", "'user id' is not a valid Python variable name"),
        ("user@id", "var", "'user@id' is not a valid Python variable name"),
        ("", "var", "'' is not a valid Python variable name"),
        # Function name validation - must use module:function syntax
        ("simple_function", "func", "must use 'module:function' syntax"),
        ("1invalid", "func", "must use 'module:function' syntax"),
        ("func-name", "func", "must use 'module:function' syntax"),
        ("func name", "func", "must use 'module:function' syntax"),
        ("func@name", "func", "must use 'module:function' syntax"),
        ("", "func", "must use 'module:function' syntax"),
        # Invalid module:function syntax
        (":function", "func", "missing module path"),
        ("module:", "func", "missing function name"),
        ("nonexistent_module:function", "func", "Cannot import module 'nonexistent_module'"),
        ("json:nonexistent_function", "func", "Function 'nonexistent_function' not found in module 'json'"),
    ],
)
def test_stage_save_invalid_names(invalid_name, field_type, expected_error):
    if field_type == "var":
        data = {"name": "test", "request": {"url": "https://api.example.com/test"}, "response": {"save": {"vars": {invalid_name: "user.id"}}}}
    else:  # func
        data = {"name": "test", "request": {"url": "https://api.example.com/test"}, "response": {"save": {"functions": [invalid_name]}}}

    with pytest.raises(ValidationError) as exc_info:
        Stage.model_validate(data)
    assert expected_error in str(exc_info.value)


@pytest.mark.parametrize(
    "invalid_jmespath,expected_error",
    [
        ("user.[invalid}", "is not a valid JMESPath expression"),
        ("user.", "is not a valid JMESPath expression"),
        ("users[0", "is not a valid JMESPath expression"),
    ],
)
def test_stage_save_invalid_jmespath_expressions(invalid_jmespath, expected_error):
    data = {"name": "test", "request": {"url": "https://api.example.com/test"}, "response": {"save": {"vars": {"user_id": invalid_jmespath}}}}
    with pytest.raises(ValidationError) as exc_info:
        Stage.model_validate(data)
    assert expected_error in str(exc_info.value)


@pytest.mark.parametrize(
    "keyword",
    ["class", "for", "if", "else", "while", "def", "return", "try", "except", "import", "from", "as", "match", "case", "_", "type"],
)
def test_stage_save_keyword_validation(keyword):
    # Test keywords for variable names
    data = {"name": "test", "request": {"url": "https://api.example.com/test"}, "response": {"save": {"vars": {keyword: "user.id"}}}}
    expected_error = f"'{keyword}' is a Python keyword and cannot be used as a variable name"

    with pytest.raises(ValidationError) as exc_info:
        Stage.model_validate(data)
    assert expected_error in str(exc_info.value)


@pytest.mark.parametrize(
    "valid_names,field_type,test_case",
    [
        # Valid variable names
        (
            {
                "__": "user.double_underscore",
                "___": "user.triple_underscore",
                "_private": "user.private",
                "__private__": "user.dunder_private"
            },
            "var",
            "underscore_variables"
        ),
        (
            {
                "user_id": "user.id",
                "userName": "user.name",
                "USER_NAME": "user.name",
                "user123": "user.id",
                "_user": "user.private",
                "__internal__": "user.internal",
                "MyClass": "user.class_name",
                "for_user": "user.for_field",
            },
            "var",
            "non_keyword_identifiers"
        ),
        (
            {
                "filtered_users": "users[?age > `18`]",
                "mapped_names": "users[*].name",
                "first_active": "users[?active][0]",
                "nested_access": "data.nested.deeply.nested.value",
                "pipe_expression": "users | [0]",
                "function_call": "length(users)",
                "conditional": "users[0] || `default`",
            },
            "var",
            "complex_jmespath"
        ),
        # Valid module:function names (only format allowed)
        (
            ["json:loads", "json:dumps", "os:getcwd", "sys:exit"],
            "func",
            "valid_module_function_names"
        ),
    ],
)
def test_stage_save_valid_names(valid_names, field_type, test_case):
    if field_type == "var":
        data = {"name": "test", "request": {"url": "https://api.example.com/test"}, "response": {"save": {"vars": valid_names}}}
        stage = Stage.model_validate(data)
        assert stage.response.save.vars == valid_names
    else:  # func
        data = {"name": "test", "request": {"url": "https://api.example.com/test"}, "response": {"save": {"functions": valid_names}}}
        stage = Stage.model_validate(data)
        assert stage.response.save.functions == valid_names


def test_stage_save_multiple_validation_errors():
    data = {"name": "test", "request": {"url": "https://api.example.com/test"}, "response": {"save": {"vars": {"1invalid": "user.id", "valid_name": "user.[invalid}"}}}}
    with pytest.raises(ValidationError) as exc_info:
        Stage.model_validate(data)
    assert "'1invalid' is not a valid Python variable name" in str(exc_info.value)


def test_save_config_standalone():
    save_config = SaveConfig(
        vars={"user_id": "user.id", "user_name": "user.name"},
        functions=["json:loads", "os:getcwd"]
    )
    assert save_config.vars["user_id"] == "user.id"
    assert save_config.vars["user_name"] == "user.name"
    assert save_config.functions == ["json:loads", "os:getcwd"]


@pytest.mark.parametrize(
    "vars_data,functions_data",
    [
        ({"user_id": "user.id"}, None),  # Only vars
        (None, ["json:loads"]),        # Only functions
        (None, None),                    # Neither
    ],
)
def test_save_config_optional_fields(vars_data, functions_data):
    save_config = SaveConfig(vars=vars_data, functions=functions_data)

    if vars_data is None:
        assert save_config.vars is None
    else:
        assert save_config.vars == vars_data

    if functions_data is None:
        assert save_config.functions is None
    else:
        assert save_config.functions == functions_data


@pytest.mark.parametrize(
    "validator_func,valid_input,expected_output",
    [
        (validate_python_variable_name, "valid_name", "valid_name"),
        (validate_python_function_name, "json:loads", "json:loads"),
        (validate_jmespath_expression, "user.id", "user.id"),
    ],
)
def test_validator_functions_valid_input(validator_func, valid_input, expected_output):
    result = validator_func(valid_input)
    assert result == expected_output


@pytest.mark.parametrize(
    "validator_func,invalid_input,expected_error",
    [
        (validate_python_variable_name, "1invalid", "'1invalid' is not a valid Python variable name"),
        (validate_python_function_name, "1invalid", "must use 'module:function' syntax"),
        (validate_jmespath_expression, "user.[invalid}", "is not a valid JMESPath expression"),
    ],
)
def test_validator_functions_invalid_input(validator_func, invalid_input, expected_error):
    with pytest.raises(ValueError, match=expected_error):
        validator_func(invalid_input)


@pytest.mark.parametrize(
    "function_name",
    [
        "json:loads",
        "json:dumps",
        "os:getcwd",
        "sys:exit"
    ],
)
def test_module_function_validation_success(function_name):
    result = validate_python_function_name(function_name)
    assert result == function_name


@pytest.mark.parametrize(
    "invalid_function_name,expected_error_fragment",
    [
        ("simple_function", "must use 'module:function' syntax"),
        (":no_module", "missing module path"),
        ("module:", "missing function name"),
        ("nonexistent_module:function", "Cannot import module 'nonexistent_module'"),
        ("json:nonexistent_function", "Function 'nonexistent_function' not found in module 'json'"),
    ],
)
def test_module_function_validation_failure(invalid_function_name, expected_error_fragment):
    with pytest.raises(ValueError, match=expected_error_fragment):
        validate_python_function_name(invalid_function_name)


# Verify model tests
@pytest.mark.parametrize(
    "status_input,expected_status,description",
    [
        (HTTPStatus.OK, HTTPStatus.OK, "with_status"),
        (None, None, "with_none"),
        ("no_args", None, "without_args"),
    ],
)
def test_verify_model_status_handling(status_input, expected_status, description):
    if description == "without_args":
        verify = Verify()
    else:
        verify = Verify(status=status_input)
    assert verify.status == expected_status


def test_verify_model_with_different_status_codes():
    status_codes = [
        HTTPStatus.OK,
        HTTPStatus.CREATED,
        HTTPStatus.NOT_FOUND,
        HTTPStatus.BAD_REQUEST,
        HTTPStatus.INTERNAL_SERVER_ERROR,
    ]

    for status_code in status_codes:
        verify = Verify(status=status_code)
        assert verify.status == status_code


def test_verify_model_with_integer_status():
    verify = Verify(status=200)
    assert verify.status == HTTPStatus.OK
    assert verify.status.value == 200


def test_verify_model_invalid_status():
    with pytest.raises(ValidationError):
        Verify(status=999)  # Invalid HTTP status code


@pytest.mark.parametrize(
    "verify_input,expected_verify_exists,expected_status",
    [
        (Verify(status=HTTPStatus.OK), True, HTTPStatus.OK),
        ({"status": 200}, True, HTTPStatus.OK),
        (None, False, None),
        ({}, True, None),
        ("no_verify", False, None),
    ],
)
def test_stage_verify_field_handling(verify_input, expected_verify_exists, expected_status):
    if verify_input == "no_verify":
        stage = Stage(name="test_stage")
    else:
        stage = Stage(name="test_stage", verify=verify_input)

    if expected_verify_exists:
        assert stage.verify is not None
        assert stage.verify.status == expected_status
    else:
        assert stage.verify is None


def test_stage_verify_field_optional():
    stage_data = {"name": "test_stage"}
    stage = Stage.model_validate(stage_data)
    assert stage.verify is None


def test_stage_with_complete_verify_data():
    stage_data = {"name": "test_stage", "request": {"url": "https://api.example.com/test"}, "response": {"verify": {"status": 201}}}
    stage = Stage.model_validate(stage_data)
    assert stage.verify is not None
    assert stage.verify.status == HTTPStatus.CREATED


# Verify functions tests
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
        ({"name": "test", "request": {"url": "https://api.example.com/test"}, "response": {"verify": {"functions": ["json:loads"]}}}, ["json:loads"]),
        ({"name": "test", "request": {"url": "https://api.example.com/test"}, "response": {"verify": {"functions": ["os:getcwd", "json:dumps"]}}}, ["os:getcwd", "json:dumps"]),
        ({"name": "test", "request": {"url": "https://api.example.com/test"}, "response": {"verify": {"functions": []}}}, []),
        ({"name": "test", "request": {"url": "https://api.example.com/test"}, "response": {"verify": {}}}, None),
        ({"name": "test", "request": {"url": "https://api.example.com/test"}}, None),
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
        "request": {"url": "https://api.example.com/test"},
        "response": {
            "verify": {
                "status": 200,
                "json": {"json.some_field": "expected_value"},
                "functions": ["json:loads"]
            }
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
    data = {"name": "test", "request": {"url": "https://api.example.com/test"}, "response": {"verify": {"functions": [invalid_name]}}}

    with pytest.raises(ValidationError):
        Stage.model_validate(data)


def test_verify_functions_valid_function_names():
    valid_names = ["json:loads", "os:getcwd"]
    data = {"name": "test", "request": {"url": "https://api.example.com/test"}, "response": {"verify": {"functions": valid_names}}}
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


# Stage method field tests
@pytest.mark.parametrize(
    "method_input,expected_method,description",
    [
        (HTTPMethod.GET, HTTPMethod.GET, "explicit_get"),
        (HTTPMethod.POST, HTTPMethod.POST, "explicit_post"),
        (HTTPMethod.PUT, HTTPMethod.PUT, "explicit_put"),
        (HTTPMethod.DELETE, HTTPMethod.DELETE, "explicit_delete"),
        (HTTPMethod.PATCH, HTTPMethod.PATCH, "explicit_patch"),
        (HTTPMethod.HEAD, HTTPMethod.HEAD, "explicit_head"),
        (HTTPMethod.OPTIONS, HTTPMethod.OPTIONS, "explicit_options"),
        (HTTPMethod.CONNECT, HTTPMethod.CONNECT, "explicit_connect"),
        (HTTPMethod.TRACE, HTTPMethod.TRACE, "explicit_trace"),
        (None, HTTPMethod.GET, "default_method"),
        ("no_method", HTTPMethod.GET, "without_method_field"),
    ],
)
def test_stage_method_field_handling(method_input, expected_method, description):
    if description == "without_method_field":
        stage_data = {"name": "test_stage"}
    else:
        stage_data = {"name": "test_stage", "request": {"method": method_input}}

    stage = Stage.model_validate(stage_data)
    assert stage.request.method == expected_method


def test_stage_method_default_value():
    stage_data = {"name": "test_stage"}
    stage = Stage.model_validate(stage_data)
    assert stage.request.method == HTTPMethod.GET


@pytest.mark.parametrize(
    "method_string,expected_method",
    [
        ("GET", HTTPMethod.GET),
        ("POST", HTTPMethod.POST),
        ("PUT", HTTPMethod.PUT),
        ("DELETE", HTTPMethod.DELETE),
        ("PATCH", HTTPMethod.PATCH),
        ("HEAD", HTTPMethod.HEAD),
        ("OPTIONS", HTTPMethod.OPTIONS),
        ("CONNECT", HTTPMethod.CONNECT),
        ("TRACE", HTTPMethod.TRACE),
    ],
)
def test_stage_method_with_string_values(method_string, expected_method):
    stage_data = {"name": "test_stage", "request": {"method": method_string}}
    stage = Stage.model_validate(stage_data)
    assert stage.request.method == expected_method


def test_stage_method_invalid_value():
    stage_data = {"name": "test_stage", "request": {"method": "INVALID_METHOD"}}
    with pytest.raises(ValidationError):
        Stage.model_validate(stage_data)


# Stage json field tests
@pytest.mark.parametrize(
    "json_input,expected_json,description",
    [
        ({"key": "value"}, {"key": "value"}, "simple_dict"),
        ({"nested": {"key": "value"}}, {"nested": {"key": "value"}}, "nested_dict"),
        ({"array": [1, 2, 3]}, {"array": [1, 2, 3]}, "dict_with_array"),
        ({"mixed": {"str": "value", "int": 42, "bool": True, "null": None}},
         {"mixed": {"str": "value", "int": 42, "bool": True, "null": None}}, "mixed_types"),
        (None, None, "explicit_none"),
        ("no_json", None, "without_json_field"),
    ],
)
def test_stage_json_field_handling(json_input, expected_json, description):
    if description == "without_json_field":
        stage_data = {"name": "test_stage"}
    else:
        stage_data = {"name": "test_stage", "request": {"json": json_input}}

    stage = Stage.model_validate(stage_data)
    assert stage.request.json == expected_json


def test_stage_json_default_value():
    stage_data = {"name": "test_stage"}
    stage = Stage.model_validate(stage_data)
    assert stage.request.json is None


@pytest.mark.parametrize(
    "json_data",
    [
        {"simple": "value"},
        {"numbers": [1, 2, 3, 4, 5]},
        {"nested": {"deep": {"structure": "value"}}},
        {"boolean": True},
        {"null_value": None},
        {"mixed": {"string": "text", "number": 42, "boolean": False, "array": [1, 2, 3]}},
        {"empty": {}},
        {"array_of_objects": [{"id": 1, "name": "item1"}, {"id": 2, "name": "item2"}]},
    ],
)
def test_stage_json_with_various_data_types(json_data):
    stage_data = {"name": "test_stage", "request": {"json": json_data}}
    stage = Stage.model_validate(stage_data)
    assert stage.request.json == json_data


@pytest.mark.parametrize(
    "json_data",
    [
        "simple_string",
        42,
        True,
        False,
        None,
        [1, 2, 3],
        ["a", "b", "c"],
    ],
)
def test_stage_json_with_serializable_values(json_data):
    """Test that JSON-serializable values are accepted."""
    stage_data = {"name": "test_stage", "request": {"json": json_data}}
    stage = Stage.model_validate(stage_data)
    assert stage.request.json == json_data


@pytest.mark.parametrize(
    "non_serializable_data,expected_error_fragment",
    [
        (lambda x: x, "Value cannot be serialized as JSON"),
        (object(), "Value cannot be serialized as JSON"),
        ({1, 2, 3}, "Value cannot be serialized as JSON"),
        ({"key": lambda x: x}, "Value cannot be serialized as JSON"),
        ({"key": object()}, "Value cannot be serialized as JSON"),
        ({"key": {1, 2, 3}}, "Value cannot be serialized as JSON"),
    ],
)
def test_stage_json_with_non_serializable_values(non_serializable_data, expected_error_fragment):
    """Test that non-JSON-serializable values are rejected."""
    stage_data = {"name": "test_stage", "request": {"json": non_serializable_data}}

    with pytest.raises(ValidationError) as exc_info:
        Stage.model_validate(stage_data)
    assert expected_error_fragment in str(exc_info.value)


def test_stage_with_method_and_json_together():
    stage_data = {
        "name": "test_stage",
        "request": {"method": HTTPMethod.POST},
        "response": {"json": {"user": {"name": "John", "email": "john@example.com"}}}
    }
    stage = Stage.model_validate(stage_data)

    assert stage.request.method == HTTPMethod.POST
    assert stage.request.json == {"user": {"name": "John", "email": "john@example.com"}}


def test_stage_with_all_optional_fields():
    stage_data = {
        "name": "complete_stage",
        "request": {"url": "https://api.example.com/users"},
        "response": {
            "method": HTTPMethod.PUT,
            "params": {"page": 1, "limit": 10},
            "headers": {"Authorization": "Bearer token", "Content-Type": "application/json"},
            "json": {"name": "Updated User", "email": "updated@example.com"},
            "save": {"vars": {"user_id": "response.id"}},
            "verify": {"status": 200, "json": {"response.success": True}}
        }
    }
    stage = Stage.model_validate(stage_data)

    assert stage.name == "complete_stage"
    assert stage.request.url == "https://api.example.com/users"
    assert stage.request.method == HTTPMethod.PUT
    assert stage.request.params == {"page": 1, "limit": 10}
    assert stage.request.headers == {"Authorization": "Bearer token", "Content-Type": "application/json"}
    assert stage.request.json == {"name": "Updated User", "email": "updated@example.com"}
    assert stage.response.save is not None
    assert stage.response.save.vars == {"user_id": "response.id"}
    assert stage.verify is not None
    assert stage.verify.status == HTTPStatus.OK
    assert stage.verify.json_data == {"response.success": True}

