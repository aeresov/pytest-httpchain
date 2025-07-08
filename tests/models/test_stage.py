import pytest
from pydantic import ValidationError

from pytest_http.models import SaveConfig, Stage, validate_jmespath_expression, validate_python_function_name, validate_python_variable_name


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
    data = {"name": "test", "save": save_data}
    stage = Stage.model_validate(data)

    assert isinstance(stage.save, SaveConfig)
    assert stage.save.vars == expected_vars
    assert stage.save.functions == expected_functions


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
        data = {"name": "test"}
    else:
        data = {"name": "test", "save": save_value}

    stage = Stage.model_validate(data)

    if expected_result is None:
        assert stage.save is None
    else:
        assert isinstance(stage.save, SaveConfig)
        assert stage.save.vars == expected_result.vars
        assert stage.save.functions == expected_result.functions


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
        data = {"name": "test", "save": {"vars": {invalid_name: "user.id"}}}
    else:  # func
        data = {"name": "test", "save": {"functions": [invalid_name]}}

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
    data = {"name": "test", "save": {"vars": {"user_id": invalid_jmespath}}}
    with pytest.raises(ValidationError) as exc_info:
        Stage.model_validate(data)
    assert expected_error in str(exc_info.value)


@pytest.mark.parametrize(
    "keyword",
    ["class", "for", "if", "else", "while", "def", "return", "try", "except", "import", "from", "as", "match", "case", "_", "type"],
)
def test_stage_save_keyword_validation(keyword):
    # Test keywords for variable names
    data = {"name": "test", "save": {"vars": {keyword: "user.id"}}}
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
        data = {"name": "test", "save": {"vars": valid_names}}
        stage = Stage.model_validate(data)
        assert stage.save.vars == valid_names
    else:  # func
        data = {"name": "test", "save": {"functions": valid_names}}
        stage = Stage.model_validate(data)
        assert stage.save.functions == valid_names


def test_stage_save_multiple_validation_errors():
    data = {"name": "test", "save": {"vars": {"1invalid": "user.id", "valid_name": "user.[invalid}"}}}
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
    assert save_config.vars == vars_data
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
    assert validator_func(valid_input) == expected_output


@pytest.mark.parametrize(
    "validator_func,invalid_input,expected_error",
    [
        (validate_python_variable_name, "1invalid", "'1invalid' is not a valid Python variable name"),
        (validate_python_function_name, "1invalid", "must use 'module:function' syntax"),
        (validate_jmespath_expression, "user.[invalid}", "is not a valid JMESPath expression"),
    ],
)
def test_validator_functions_invalid_input(validator_func, invalid_input, expected_error):
    with pytest.raises(ValueError) as exc_info:
        validator_func(invalid_input)
    assert expected_error in str(exc_info.value)


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
    with pytest.raises(ValueError) as exc_info:
        validate_python_function_name(invalid_function_name)
    assert expected_error_fragment in str(exc_info.value)
