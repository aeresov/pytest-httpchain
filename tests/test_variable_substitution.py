import json

import pytest

from pytest_http.pytest_plugin import VariableSubstitutionError, substitute_stage_variables, substitute_variables


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


# Tests for stage-by-stage variable substitution


def test_substitute_stage_variables_basic():
    stage_data = {"name": "test_stage", "url": "https://api.example.com/users/$user_id", "data": {"message": "$greeting"}}
    variables = {"user_id": 123, "greeting": "Hello"}

    result = substitute_stage_variables(stage_data, variables)

    expected = {"name": "test_stage", "url": "https://api.example.com/users/123", "data": {"message": "Hello"}}
    assert result == expected


def test_substitute_stage_variables_mixed_fixtures_and_saved():
    stage_data = {"name": "second_stage", "url": "https://api.example.com/users/$user_id/posts", "headers": {"Authorization": "Bearer $token"}, "params": {"limit": "$page_size"}}
    variables = {
        "user_id": 456,  # from previous stage
        "token": "abc123",  # from fixture
        "page_size": 10,  # from fixture
    }

    result = substitute_stage_variables(stage_data, variables)

    expected = {"name": "second_stage", "url": "https://api.example.com/users/456/posts", "headers": {"Authorization": "Bearer abc123"}, "params": {"limit": 10}}
    assert result == expected


def test_substitute_stage_variables_nested_data():
    stage_data = {"name": "complex_stage", "data": {"user": {"id": "$user_id", "profile": "$user_profile"}, "settings": {"theme": "$theme", "notifications": "$notifications"}}}
    variables = {"user_id": 789, "user_profile": {"name": "John", "email": "john@example.com"}, "theme": "dark", "notifications": True}

    result = substitute_stage_variables(stage_data, variables)

    expected = {
        "name": "complex_stage",
        "data": {"user": {"id": 789, "profile": {"name": "John", "email": "john@example.com"}}, "settings": {"theme": "dark", "notifications": True}},
    }
    assert result == expected


def test_substitute_stage_variables_no_variables():
    stage_data = {"name": "static_stage", "url": "https://api.example.com/status", "data": {"check": "health"}}
    variables = {}

    result = substitute_stage_variables(stage_data, variables)

    assert result == stage_data


def test_substitute_stage_variables_partial_substitution():
    stage_data = {"name": "partial_stage", "url": "https://api.example.com/users/$user_id", "data": {"message": "$missing_var"}}
    variables = {"user_id": 123}

    result = substitute_stage_variables(stage_data, variables)

    expected = {
        "name": "partial_stage",
        "url": "https://api.example.com/users/123",
        "data": {"message": "$missing_var"},  # This variable is not substituted
    }
    assert result == expected


def test_substitute_stage_variables_error_handling():
    stage_data = {"data": "$invalid"}

    class UnserializableObject:
        def __repr__(self):
            raise Exception("Cannot serialize")

    variables = {"invalid": UnserializableObject()}

    with pytest.raises(VariableSubstitutionError, match="Failed to substitute variables in stage"):
        substitute_stage_variables(stage_data, variables)
