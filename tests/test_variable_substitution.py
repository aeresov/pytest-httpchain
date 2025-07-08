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
        ('{"name": "$name", "age": "$age", "city": "$city"}', {"name": "Bob", "age": 30, "city": "New York"}, {"name": "Bob", "age": 30, "city": "New York"}),
        ('{"data": "$nullable", "id": 1}', {"nullable": None}, {"data": None, "id": 1}),
        ('{"message": "$greeting", "symbol": "@"}', {"greeting": "Hello, World!"}, {"message": "Hello, World!", "symbol": "@"}),
        ('{"var": "$var", "variable": "$variable"}', {"var": "short", "variable": "long_value"}, {"var": "short", "variable": "long_value"}),
        # No substitutions needed
        ('{"name": "static", "value": 123}', {"unused": "value"}, {"name": "static", "value": 123}),
        ('{"name": "test", "value": 42}', {}, {"name": "test", "value": 42}),
    ],
)
def test_substitute_variables_basic_cases(json_text: str, fixtures: dict, expected: dict):
    result = substitute_variables(json_text, fixtures)
    parsed_result = json.loads(result)
    assert parsed_result == expected


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
    fixtures = {
        "user_name": "Alice",
        "profile": {"age": 25, "email": "alice@example.com"},
        "config": {"theme": "dark", "notifications": True}
    }
    result = substitute_variables(json_text, fixtures)
    parsed_result = json.loads(result)
    expected = {
        "user": {
            "name": "Alice",
            "profile": {"age": 25, "email": "alice@example.com"}
        },
        "settings": {"theme": "dark", "notifications": True}
    }
    assert parsed_result == expected


@pytest.mark.parametrize(
    "stage_data,variables,expected",
    [
        (
            {"name": "test_stage", "url": "https://api.example.com/users/$user_id", "data": {"message": "$greeting"}},
            {"user_id": 123, "greeting": "Hello"},
            {"name": "test_stage", "url": "https://api.example.com/users/123", "data": {"message": "Hello"}},
        ),
        (
            {
                "name": "auth_stage",
                "url": "https://api.example.com/users/$user_id/posts",
                "headers": {"Authorization": "Bearer $token"},
                "params": {"limit": "$page_size"}
            },
            {"user_id": 456, "token": "abc123", "page_size": 10},
            {
                "name": "auth_stage",
                "url": "https://api.example.com/users/456/posts",
                "headers": {"Authorization": "Bearer abc123"},
                "params": {"limit": 10}
            },
        ),
        # No substitutions
        (
            {"name": "static_stage", "url": "https://api.example.com/status", "data": {"check": "health"}},
            {},
            {"name": "static_stage", "url": "https://api.example.com/status", "data": {"check": "health"}},
        ),
        # Partial substitutions
        (
            {"name": "partial_stage", "url": "https://api.example.com/users/$user_id", "data": {"message": "$missing_var"}},
            {"user_id": 123},
            {"name": "partial_stage", "url": "https://api.example.com/users/123", "data": {"message": "$missing_var"}},
        ),
    ],
)
def test_substitute_stage_variables_basic_cases(stage_data: dict, variables: dict, expected: dict):
    result = substitute_stage_variables(stage_data, variables)
    assert result == expected


def test_substitute_stage_variables_complex_nested():
    stage_data = {
        "name": "complex_stage",
        "data": {
            "user": {"id": "$user_id", "profile": "$user_profile"},
            "settings": {"theme": "$theme", "notifications": "$notifications"}
        }
    }
    variables = {
        "user_id": 789,
        "user_profile": {"name": "John", "email": "john@example.com"},
        "theme": "dark",
        "notifications": True
    }

    result = substitute_stage_variables(stage_data, variables)

    expected = {
        "name": "complex_stage",
        "data": {
            "user": {"id": 789, "profile": {"name": "John", "email": "john@example.com"}},
            "settings": {"theme": "dark", "notifications": True}
        },
    }
    assert result == expected


@pytest.mark.parametrize(
    "function_name,data,variables,expected_error",
    [
        (
            "substitute_variables",
            '{"data": "$invalid"}',
            {"invalid": object()},
            "Failed to substitute variables"
        ),
        (
            "substitute_stage_variables",
            {"data": "$invalid"},
            {"invalid": object()},
            "Failed to substitute variables in stage"
        ),
    ],
)
def test_substitution_error_handling(function_name, data, variables, expected_error):
    function_map = {
        "substitute_variables": substitute_variables,
        "substitute_stage_variables": substitute_stage_variables,
    }

    func = function_map[function_name]
    with pytest.raises(VariableSubstitutionError, match=expected_error):
        func(data, variables)
