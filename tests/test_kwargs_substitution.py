"""
Test the kwargs substitution functionality.
"""

import json
from unittest.mock import Mock

from pytest_http.pytest_plugin import substitute_kwargs_variables


def test_substitute_kwargs_variables_basic():
    """Test basic kwargs substitution."""
    kwargs = {
        "expected_status": "$status",
        "expected_text": "$text",
        "case_sensitive": True
    }
    
    variables = {
        "status": 200,
        "text": "Hello World"
    }
    
    result = substitute_kwargs_variables(kwargs, variables)
    
    expected = {
        "expected_status": 200,
        "expected_text": "Hello World",
        "case_sensitive": True
    }
    
    assert result == expected


def test_substitute_kwargs_variables_none():
    """Test kwargs substitution with None kwargs."""
    result = substitute_kwargs_variables(None, {"var": "value"})
    assert result is None


def test_substitute_kwargs_variables_no_substitution():
    """Test kwargs substitution when no variables are used."""
    kwargs = {
        "param1": "value1",
        "param2": 42,
        "param3": True
    }
    
    variables = {"unused": "value"}
    
    result = substitute_kwargs_variables(kwargs, variables)
    
    assert result == kwargs


def test_substitute_kwargs_variables_mixed_types():
    """Test kwargs substitution with mixed data types."""
    kwargs = {
        "string_param": "$string_var",
        "number_param": "$number_var",
        "boolean_param": "$boolean_var",
        "array_param": "$array_var",
        "object_param": "$object_var"
    }
    
    variables = {
        "string_var": "test string",
        "number_var": 42,
        "boolean_var": True,
        "array_var": [1, 2, 3],
        "object_var": {"key": "value"}
    }
    
    result = substitute_kwargs_variables(kwargs, variables)
    
    expected = {
        "string_param": "test string",
        "number_param": 42,
        "boolean_param": True,
        "array_param": [1, 2, 3],
        "object_param": {"key": "value"}
    }
    
    assert result == expected


def test_substitute_kwargs_variables_nested():
    """Test kwargs substitution with nested structures."""
    kwargs = {
        "fields": ["$field1", "$field2", "static_field"],
        "config": {
            "default_value": "$default",
            "timeout": "$timeout"
        }
    }
    
    variables = {
        "field1": "slideshow.title",
        "field2": "slideshow.author",
        "default": "unknown",
        "timeout": 5.0
    }
    
    result = substitute_kwargs_variables(kwargs, variables)
    
    expected = {
        "fields": ["slideshow.title", "slideshow.author", "static_field"],
        "config": {
            "default_value": "unknown",
            "timeout": 5.0
        }
    }
    
    assert result == expected


def test_substitute_kwargs_variables_quoted_strings():
    """Test kwargs substitution with quoted string placeholders."""
    kwargs = {
        "expected_text": '"$text_var"',
        "field_path": '"$path_var"'
    }
    
    variables = {
        "text_var": "Hello World",
        "path_var": "slideshow.title"
    }
    
    result = substitute_kwargs_variables(kwargs, variables)
    
    expected = {
        "expected_text": "Hello World",
        "field_path": "slideshow.title"
    }
    
    assert result == expected


def test_substitute_kwargs_variables_partial_substitution():
    """Test kwargs substitution where only some values are substituted."""
    kwargs = {
        "param1": "$var1",
        "param2": "static_value",
        "param3": "$var2",
        "param4": 42
    }
    
    variables = {
        "var1": "substituted_value",
        "var2": "another_value"
    }
    
    result = substitute_kwargs_variables(kwargs, variables)
    
    expected = {
        "param1": "substituted_value",
        "param2": "static_value",
        "param3": "another_value",
        "param4": 42
    }
    
    assert result == expected


def test_substitute_kwargs_variables_empty_variables():
    """Test kwargs substitution with empty variables dict."""
    kwargs = {
        "param1": "$var1",
        "param2": "static_value"
    }
    
    variables = {}
    
    result = substitute_kwargs_variables(kwargs, variables)
    
    # Should return original kwargs when no variables are available
    assert result == kwargs


def test_substitute_kwargs_variables_complex_json():
    """Test kwargs substitution with complex JSON structures."""
    kwargs = {
        "config": {
            "filters": [
                {"field": "$field1", "value": "$value1"},
                {"field": "$field2", "value": "$value2"}
            ],
            "options": {
                "timeout": "$timeout",
                "retries": 3
            }
        }
    }
    
    variables = {
        "field1": "title",
        "value1": "Sample",
        "field2": "author",
        "value2": "Yours Truly",
        "timeout": 5.0
    }
    
    result = substitute_kwargs_variables(kwargs, variables)
    
    expected = {
        "config": {
            "filters": [
                {"field": "title", "value": "Sample"},
                {"field": "author", "value": "Yours Truly"}
            ],
            "options": {
                "timeout": 5.0,
                "retries": 3
            }
        }
    }
    
    assert result == expected