import json

import pytest

from pytest_http.pytest_plugin import VariableSubstitutionError, substitute_stage_variables, substitute_variables


@pytest.mark.parametrize(
    "json_text,fixtures,expected,description",
    [
        # Basic variable substitution
        ('{"name": "$user_name", "age": 25}', {"user_name": "Alice"}, {"name": "Alice", "age": 25}, "basic_string"),
        ('{"count": "$number", "active": true}', {"number": 42}, {"count": 42, "active": True}, "basic_number"),
        ('{"enabled": "$flag", "name": "test"}', {"flag": True}, {"enabled": True, "name": "test"}, "basic_boolean"),
        ('{"data": "$nullable", "id": 1}', {"nullable": None}, {"data": None, "id": 1}, "null_value"),
        
        # Complex data types
        ('{"config": "$settings", "version": 1}', {"settings": {"host": "localhost", "port": 8080}}, {"config": {"host": "localhost", "port": 8080}, "version": 1}, "dict_value"),
        ('{"items": "$list_data", "total": 3}', {"list_data": ["a", "b", "c"]}, {"items": ["a", "b", "c"], "total": 3}, "list_value"),
        
        # Multiple variables
        ('{"name": "$name", "age": "$age", "city": "$city"}', {"name": "Bob", "age": 30, "city": "New York"}, {"name": "Bob", "age": 30, "city": "New York"}, "multiple_vars"),
        ('{"var": "$var", "variable": "$variable"}', {"var": "short", "variable": "long_value"}, {"var": "short", "variable": "long_value"}, "similar_var_names"),
        
        # Special characters and edge cases
        ('{"message": "$greeting", "symbol": "@"}', {"greeting": "Hello, World!"}, {"message": "Hello, World!", "symbol": "@"}, "special_chars"),
        
        # No substitutions needed
        ('{"name": "static", "value": 123}', {"unused": "value"}, {"name": "static", "value": 123}, "no_substitution"),
        ('{"name": "test", "value": 42}', {}, {"name": "test", "value": 42}, "empty_fixtures"),
    ],
)
def test_substitute_variables_comprehensive(json_text: str, fixtures: dict, expected: dict, description: str):
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
    "stage_data,variables,expected,description",
    [
        # Basic stage substitution
        (
            {"name": "test_stage", "url": "https://api.example.com/users/$user_id", "data": {"message": "$greeting"}},
            {"user_id": 123, "greeting": "Hello"},
            {"name": "test_stage", "url": "https://api.example.com/users/123", "data": {"message": "Hello"}},
            "basic_stage"
        ),
        
        # Complex stage with multiple fields
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
            "complex_stage"
        ),
        
        # No substitutions
        (
            {"name": "static_stage", "url": "https://api.example.com/status", "data": {"check": "health"}},
            {},
            {"name": "static_stage", "url": "https://api.example.com/status", "data": {"check": "health"}},
            "no_substitution"
        ),
        
        # Partial substitutions
        (
            {"name": "partial_stage", "url": "https://api.example.com/users/$user_id", "data": {"message": "$missing_var"}},
            {"user_id": 123},
            {"name": "partial_stage", "url": "https://api.example.com/users/123", "data": {"message": "$missing_var"}},
            "partial_substitution"
        ),
    ],
)
def test_substitute_stage_variables_comprehensive(stage_data: dict, variables: dict, expected: dict, description: str):
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