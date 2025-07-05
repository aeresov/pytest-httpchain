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
