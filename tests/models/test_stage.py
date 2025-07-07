import pytest
from pydantic import ValidationError

from pytest_http.models import Stage, validate_jmespath_expression, validate_python_variable_name


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


def test_stage_with_valid_save_field():
    data = {
        "name": "test",
        "save": {"user_id": "user.id", "user_name": "user.name", "first_item": "items[0]", "_private_var": "data._private", "complex_path": "users[*].profile.name"},
    }
    stage = Stage.model_validate(data)
    assert stage.save is not None
    assert stage.save["user_id"] == "user.id"
    assert stage.save["user_name"] == "user.name"
    assert stage.save["first_item"] == "items[0]"
    assert stage.save["_private_var"] == "data._private"
    assert stage.save["complex_path"] == "users[*].profile.name"


@pytest.mark.parametrize(
    "save_value,expected,description",
    [
        (None, None, "with_none"),
        ({}, {}, "with_empty_dict"),
        ("no_save", None, "without_save_field"),
    ],
)
def test_stage_save_field_optional_states(save_value, expected, description):
    if description == "without_save_field":
        data = {"name": "test"}
    else:
        data = {"name": "test", "save": save_value}
    stage = Stage.model_validate(data)
    assert stage.save == expected


@pytest.mark.parametrize(
    "invalid_key,expected_error",
    [
        ("1invalid", "'1invalid' is not a valid Python variable name"),
        ("user-id", "'user-id' is not a valid Python variable name"),
        ("user id", "'user id' is not a valid Python variable name"),
        ("user@id", "'user@id' is not a valid Python variable name"),
        ("", "'' is not a valid Python variable name"),
    ],
)
def test_stage_save_invalid_python_variable_names(invalid_key, expected_error):
    data = {"name": "test", "save": {invalid_key: "user.id"}}
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
    data = {"name": "test", "save": {"user_id": invalid_jmespath}}
    with pytest.raises(ValidationError) as exc_info:
        Stage.model_validate(data)
    assert expected_error in str(exc_info.value)


@pytest.mark.parametrize("keyword", ["class", "for", "if", "else", "while", "def", "return", "try", "except", "import", "from", "as", "match", "case", "_", "type"])
def test_stage_save_invalid_keywords(keyword):
    data = {"name": "test", "save": {keyword: "user.id"}}
    with pytest.raises(ValidationError) as exc_info:
        Stage.model_validate(data)
    assert f"'{keyword}' is a Python keyword and cannot be used as a variable name" in str(exc_info.value)


def test_stage_save_valid_underscore_variable_names():
    data = {
        "name": "test",
        "save": {"__": "user.double_underscore", "___": "user.triple_underscore", "_private": "user.private", "__private__": "user.dunder_private"},
    }
    stage = Stage.model_validate(data)
    assert stage.save["__"] == "user.double_underscore"
    assert stage.save["___"] == "user.triple_underscore"
    assert stage.save["_private"] == "user.private"
    assert stage.save["__private__"] == "user.dunder_private"


def test_stage_save_valid_complex_jmespath_expressions():
    data = {
        "name": "test",
        "save": {
            "filtered_users": "users[?age > `18`]",
            "mapped_names": "users[*].name",
            "first_active": "users[?active][0]",
            "nested_access": "data.nested.deeply.nested.value",
            "pipe_expression": "users | [0]",
            "function_call": "length(users)",
            "conditional": "users[0] || `default`",
        },
    }
    stage = Stage.model_validate(data)
    assert stage.save["filtered_users"] == "users[?age > `18`]"
    assert stage.save["mapped_names"] == "users[*].name"
    assert stage.save["first_active"] == "users[?active][0]"
    assert stage.save["nested_access"] == "data.nested.deeply.nested.value"
    assert stage.save["pipe_expression"] == "users | [0]"
    assert stage.save["function_call"] == "length(users)"
    assert stage.save["conditional"] == "users[0] || `default`"


def test_stage_save_multiple_validation_errors():
    data = {"name": "test", "save": {"1invalid": "user.id", "valid_name": "user.[invalid}"}}
    with pytest.raises(ValidationError) as exc_info:
        Stage.model_validate(data)
    assert "'1invalid' is not a valid Python variable name" in str(exc_info.value)


def test_stage_save_keyword_vs_invalid_variable_error_precedence():
    data = {"name": "test", "save": {"1class": "user.id"}}
    with pytest.raises(ValidationError) as exc_info:
        Stage.model_validate(data)
    assert "'1class' is not a valid Python variable name" in str(exc_info.value)


def test_stage_save_valid_non_keyword_identifiers():
    data = {
        "name": "test",
        "save": {
            "user_id": "user.id",
            "userName": "user.name",
            "USER_NAME": "user.name",
            "user123": "user.id",
            "_user": "user.private",
            "__internal__": "user.internal",
            "MyClass": "user.class_name",
            "for_user": "user.for_field",
        },
    }
    stage = Stage.model_validate(data)
    assert len(stage.save) == 8
    assert stage.save["user_id"] == "user.id"
    assert stage.save["MyClass"] == "user.class_name"
    assert stage.save["for_user"] == "user.for_field"


@pytest.mark.parametrize(
    "validator_func,valid_input,expected_output",
    [
        (validate_python_variable_name, "valid_name", "valid_name"),
        (validate_jmespath_expression, "user.id", "user.id"),
    ],
)
def test_individual_annotated_types_valid(validator_func, valid_input, expected_output):
    assert validator_func(valid_input) == expected_output


@pytest.mark.parametrize(
    "validator_func,invalid_input,expected_error",
    [
        (validate_python_variable_name, "1invalid", "'1invalid' is not a valid Python variable name"),
        (validate_jmespath_expression, "user.[invalid}", "is not a valid JMESPath expression"),
    ],
)
def test_individual_annotated_types_invalid(validator_func, invalid_input, expected_error):
    with pytest.raises(ValueError) as exc_info:
        validator_func(invalid_input)
    assert expected_error in str(exc_info.value)
