import json

import pytest

from pytest_http.pytest_plugin import VariableSubstitutionError, substitute_variables


@pytest.mark.parametrize(
    "json_text,fixtures,expected",
    [
        ('{"name": "$user_name", "age": 25}', {"user_name": "Alice"}, {"name": "Alice", "age": 25}),
        ('{"count": "$number", "active": true}', {"number": 42}, {"count": 42, "active": True}),
        ('{"enabled": "$flag", "name": "test"}', {"flag": True}, {"enabled": True, "name": "test"}),
        ('{"config": "$settings", "version": 1}', {"settings": {"host": "localhost", "port": 8080}}, {"config": {"host": "localhost", "port": 8080}, "version": 1}),
        ('{"items": "$list_data", "total": 3}', {"list_data": ["a", "b", "c"]}, {"items": ["a", "b", "c"], "total": 3}),
    ],
)
def test_substitute_variables_with_different_types(json_text: str, fixtures: dict, expected: dict):
    result = substitute_variables(json_text, fixtures)
    parsed_result = json.loads(result)
    assert parsed_result == expected


def test_substitute_variables_multiple_substitutions():
    json_text = '{"name": "$name", "age": "$age", "city": "$city"}'
    fixtures = {"name": "Bob", "age": 30, "city": "New York"}
    result = substitute_variables(json_text, fixtures)
    parsed_result = json.loads(result)
    assert parsed_result == {"name": "Bob", "age": 30, "city": "New York"}


def test_substitute_variables_no_substitutions():
    json_text = '{"name": "static", "value": 123}'
    fixtures = {"unused": "value"}
    result = substitute_variables(json_text, fixtures)
    parsed_result = json.loads(result)
    assert parsed_result == {"name": "static", "value": 123}


def test_substitute_variables_empty_fixtures():
    json_text = '{"name": "test", "value": 42}'
    fixtures = {}
    result = substitute_variables(json_text, fixtures)
    parsed_result = json.loads(result)
    assert parsed_result == {"name": "test", "value": 42}


def test_substitute_variables_with_none_value():
    json_text = '{"data": "$nullable", "id": 1}'
    fixtures = {"nullable": None}
    result = substitute_variables(json_text, fixtures)
    parsed_result = json.loads(result)
    assert parsed_result == {"data": None, "id": 1}


def test_substitute_variables_complex_nested():
    json_text = """
    {
        "user": {
            "name": "$user_name",
            "profile": "$profile"
        },
        "settings": "$config"
    }
    """
    fixtures = {"user_name": "Alice", "profile": {"age": 25, "email": "alice@example.com"}, "config": {"theme": "dark", "notifications": True}}
    result = substitute_variables(json_text, fixtures)
    parsed_result = json.loads(result)
    expected = {"user": {"name": "Alice", "profile": {"age": 25, "email": "alice@example.com"}}, "settings": {"theme": "dark", "notifications": True}}
    assert parsed_result == expected


def test_substitute_variables_with_special_characters():
    json_text = '{"message": "$greeting", "symbol": "@"}'
    fixtures = {"greeting": "Hello, World!"}
    result = substitute_variables(json_text, fixtures)
    parsed_result = json.loads(result)
    assert parsed_result == {"message": "Hello, World!", "symbol": "@"}


def test_substitute_variables_error_handling():
    json_text = '{"data": "$invalid"}'

    class UnserializableObject:
        def __repr__(self):
            raise Exception("Cannot serialize")

    fixtures = {"invalid": UnserializableObject()}

    with pytest.raises(VariableSubstitutionError, match="Failed to substitute variables"):
        substitute_variables(json_text, fixtures)


def test_substitute_variables_partial_match():
    json_text = '{"var": "$var", "variable": "$variable"}'
    fixtures = {"var": "short", "variable": "long_value"}
    result = substitute_variables(json_text, fixtures)
    parsed_result = json.loads(result)
    assert parsed_result == {"var": "short", "variable": "long_value"}
