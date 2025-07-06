import pytest
from pydantic import ValidationError

from pytest_http.models import Stage


@pytest.mark.parametrize(
    "name,data,expected_name,expected_data",
    [
        ("test_stage", "string_value", "test_stage", "string_value"),
        ("numeric_stage", 42, "numeric_stage", 42),
        ("dict_stage", {"key": "value", "number": 123}, "dict_stage", {"key": "value", "number": 123}),
        ("list_stage", [1, 2, 3, "four"], "list_stage", [1, 2, 3, "four"]),
        ("bool_stage", True, "bool_stage", True),
        ("null_stage", None, "null_stage", None),
    ],
)
def test_stage_with_different_data_types(name: str, data, expected_name: str, expected_data):
    data_dict = {"name": name, "data": data}
    stage = Stage.model_validate(data_dict)
    assert stage.name == expected_name
    assert stage.data == expected_data


def test_stage_missing_name():
    data = {"data": "some_data"}
    with pytest.raises(ValidationError, match="name"):
        Stage.model_validate(data)


def test_stage_missing_data():
    data = {"name": "test_stage"}
    with pytest.raises(ValidationError, match="data"):
        Stage.model_validate(data)


def test_stage_empty_name():
    data = {"name": "", "data": "some_data"}
    stage = Stage.model_validate(data)
    assert stage.name == ""
    assert stage.data == "some_data"


def test_stage_with_valid_save_field():
    data = {
        "name": "test",
        "data": "data",
        "save": {"user_id": "user.id", "user_name": "user.name", "first_item": "items[0]", "_private_var": "data._private", "complex_path": "users[*].profile.name"},
    }
    stage = Stage.model_validate(data)
    assert stage.save is not None
    assert stage.save["user_id"] == "user.id"
    assert stage.save["user_name"] == "user.name"
    assert stage.save["first_item"] == "items[0]"
    assert stage.save["_private_var"] == "data._private"
    assert stage.save["complex_path"] == "users[*].profile.name"


@pytest.mark.parametrize("save_value,expected", [
    (None, None),
    ({}, {}),
])
def test_stage_save_field_optional_states(save_value, expected):
    data = {"name": "test", "data": "data", "save": save_value}
    stage = Stage.model_validate(data)
    assert stage.save == expected


def test_stage_without_save_field():
    data = {"name": "test", "data": "data"}
    stage = Stage.model_validate(data)
    assert stage.save is None


@pytest.mark.parametrize("invalid_key,expected_error", [
    ("1invalid", "'1invalid' is not a valid Python variable name"),
    ("user-id", "'user-id' is not a valid Python variable name"),
    ("user id", "'user id' is not a valid Python variable name"),
    ("user@id", "'user@id' is not a valid Python variable name"),
    ("", "'' is not a valid Python variable name"),
])
def test_stage_save_invalid_python_variable_names(invalid_key, expected_error):
    data = {"name": "test", "data": "data", "save": {invalid_key: "user.id"}}
    with pytest.raises(ValidationError) as exc_info:
        Stage.model_validate(data)
    assert expected_error in str(exc_info.value)


@pytest.mark.parametrize("invalid_jmespath", [
    "user.[invalid}",
    "user.",
    "users[0",
])
def test_stage_save_invalid_jmespath_expressions(invalid_jmespath):
    data = {"name": "test", "data": "data", "save": {"user_id": invalid_jmespath}}
    with pytest.raises(ValidationError) as exc_info:
        Stage.model_validate(data)
    assert "is not a valid JMESPath expression" in str(exc_info.value)


@pytest.mark.parametrize("keyword", [
    "class", "for", "if", "else", "while", "def", "return", "try", "except", "import", "from", "as",
    "match", "case", "_", "type"
])
def test_stage_save_invalid_keywords(keyword):
    data = {
        "name": "test",
        "data": "data",
        "save": {keyword: "user.id"}
    }
    with pytest.raises(ValidationError) as exc_info:
        Stage.model_validate(data)
    assert f"'{keyword}' is a Python keyword and cannot be used as a variable name" in str(exc_info.value)


def test_stage_save_valid_underscore_variable_names():
    data = {
        "name": "test",
        "data": "data",
        "save": {
            "__": "user.double_underscore",
            "___": "user.triple_underscore",
            "_private": "user.private",
            "__private__": "user.dunder_private"
        }
    }
    stage = Stage.model_validate(data)
    assert stage.save["__"] == "user.double_underscore"
    assert stage.save["___"] == "user.triple_underscore"
    assert stage.save["_private"] == "user.private"
    assert stage.save["__private__"] == "user.dunder_private"


def test_stage_save_valid_complex_jmespath_expressions():
    data = {
        "name": "test",
        "data": "data",
        "save": {
            "filtered_users": "users[?age > `18`]",
            "mapped_names": "users[*].name",
            "first_active": "users[?active][0]",
            "nested_access": "data.nested.deeply.nested.value",
            "pipe_expression": "users | [0]",
            "function_call": "length(users)",
            "conditional": "users[0] || `default`"
        }
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
    data = {
        "name": "test",
        "data": "data",
        "save": {
            "1invalid": "user.id",
            "valid_name": "user.[invalid}"
        }
    }
    with pytest.raises(ValidationError) as exc_info:
        Stage.model_validate(data)
    assert "'1invalid' is not a valid Python variable name" in str(exc_info.value)


def test_stage_save_keyword_vs_invalid_variable_error_precedence():
    data = {
        "name": "test",
        "data": "data",
        "save": {"1class": "user.id"}
    }
    with pytest.raises(ValidationError) as exc_info:
        Stage.model_validate(data)
    assert "'1class' is not a valid Python variable name" in str(exc_info.value)


def test_stage_save_valid_non_keyword_identifiers():
    data = {
        "name": "test",
        "data": "data",
        "save": {
            "user_id": "user.id",
            "userName": "user.name",
            "USER_NAME": "user.name",
            "user123": "user.id",
            "_user": "user.private",
            "__internal__": "user.internal",
            "MyClass": "user.class_name",
            "for_user": "user.for_field",
        }
    }
    stage = Stage.model_validate(data)
    assert len(stage.save) == 8
    assert stage.save["user_id"] == "user.id"
    assert stage.save["MyClass"] == "user.class_name"
    assert stage.save["for_user"] == "user.for_field"


def test_individual_annotated_types():
    from pydantic import BaseModel

    from pytest_http.models import JMESPathExpression, ValidPythonVariableName

    class TestModel(BaseModel):
        var_name: ValidPythonVariableName
        jmes_expr: JMESPathExpression

    valid_data = {"var_name": "valid_name", "jmes_expr": "user.id"}
    model = TestModel.model_validate(valid_data)
    assert model.var_name == "valid_name"
    assert model.jmes_expr == "user.id"

    with pytest.raises(ValidationError) as exc_info:
        TestModel.model_validate({"var_name": "1invalid", "jmes_expr": "user.id"})
    assert "'1invalid' is not a valid Python variable name" in str(exc_info.value)

    with pytest.raises(ValidationError) as exc_info:
        TestModel.model_validate({"var_name": "valid_name", "jmes_expr": "user.[invalid}"})
    assert "is not a valid JMESPath expression" in str(exc_info.value)
