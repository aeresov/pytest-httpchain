# Kwargs Functionality for Verify and Save Functions

This document describes the new kwargs functionality that allows user functions in "verify" and "save" blocks to be called with arbitrary keyword arguments, including support for variable substitution.

## Overview

Previously, user functions in verify and save blocks could only be called with a single `response` argument. Now, you can optionally pass arbitrary keyword arguments to these functions, making them more flexible and reusable. Additionally, kwargs values support variable substitution, allowing you to use fixtures and saved variables.

## Backward Compatibility

The new functionality is fully backward compatible. Existing function calls that use string function names will continue to work exactly as before.

## New Function Call Format

### FunctionCall Object

Instead of just a string function name, you can now use a `FunctionCall` object:

```json
{
  "function": "module:function_name",
  "kwargs": {
    "param1": "value1",
    "param2": 42,
    "param3": true
  }
}
```

### Mixed Format Support

You can mix string function names and FunctionCall objects in the same list:

```json
{
  "functions": [
    "module:simple_function",
    {
      "function": "module:function_with_kwargs",
      "kwargs": {
        "expected_status": 200,
        "timeout": 5.0
      }
    }
  ]
}
```

## Variable Substitution in Kwargs

Kwargs values support variable substitution using the `$variable_name` syntax. This allows you to use:

1. **Fixtures** - Variables passed to the test function
2. **Saved variables** - Variables saved in previous stages or in the same stage
3. **Mixed values** - Combine static values with variable substitutions

### Examples

#### Using Fixtures in Kwargs

```json
{
  "fixtures": ["expected_status", "expected_text"],
  "stages": [
    {
      "name": "test_with_fixtures",
      "url": "https://httpbin.org/json",
      "verify": {
        "functions": [
          {
            "function": "test_kwargs_helpers:verify_response_status_custom",
            "kwargs": {
              "expected_status": "$expected_status"
            }
          },
          {
            "function": "test_kwargs_helpers:verify_response_contains_text",
            "kwargs": {
              "expected_text": "$expected_text",
              "case_sensitive": false
            }
          }
        ]
      }
    }
  ]
}
```

#### Using Saved Variables in Kwargs

```json
{
  "stages": [
    {
      "name": "test_with_saved_variables",
      "url": "https://httpbin.org/json",
      "save": {
        "vars": {
          "extracted_title": "json.slideshow.title",
          "extracted_author": "json.slideshow.author"
        }
      },
      "verify": {
        "functions": [
          {
            "function": "test_kwargs_helpers:verify_response_contains_text",
            "kwargs": {
              "expected_text": "$extracted_title",
              "case_sensitive": true
            }
          }
        ]
      }
    }
  ]
}
```

#### Using Variables from Same Stage

```json
{
  "stages": [
    {
      "name": "test_same_stage_variables",
      "url": "https://httpbin.org/json",
      "save": {
        "vars": {
          "title": "json.slideshow.title"
        },
        "functions": [
          {
            "function": "test_kwargs_helpers:extract_custom_data",
            "kwargs": {
              "field_path": "slideshow.author",
              "default_value": "$title"
            }
          }
        ]
      }
    }
  ]
}
```

#### Mixed Variable and Static Values

```json
{
  "fixtures": ["field_path"],
  "stages": [
    {
      "name": "test_mixed_values",
      "url": "https://httpbin.org/json",
      "save": {
        "functions": [
          {
            "function": "test_kwargs_helpers:extract_multiple_fields",
            "kwargs": {
              "fields": ["$field_path", "slideshow.author", "slideshow.date"]
            }
          }
        ]
      }
    }
  ]
}
```

## Examples

### Verify Functions with Kwargs

```json
{
  "stages": [
    {
      "name": "test_verify_with_kwargs",
      "url": "https://httpbin.org/json",
      "verify": {
        "functions": [
          {
            "function": "test_kwargs_helpers:verify_response_status_custom",
            "kwargs": {
              "expected_status": 200
            }
          },
          {
            "function": "test_kwargs_helpers:verify_response_contains_text",
            "kwargs": {
              "expected_text": "Sample Slide Show",
              "case_sensitive": false
            }
          }
        ]
      }
    }
  ]
}
```

### Save Functions with Kwargs

```json
{
  "stages": [
    {
      "name": "test_save_with_kwargs",
      "url": "https://httpbin.org/json",
      "save": {
        "functions": [
          {
            "function": "test_kwargs_helpers:extract_custom_data",
            "kwargs": {
              "field_path": "slideshow.author",
              "default_value": "unknown_author"
            }
          },
          {
            "function": "test_kwargs_helpers:extract_multiple_fields",
            "kwargs": {
              "fields": ["slideshow.title", "slideshow.author", "slideshow.date"]
            }
          }
        ]
      }
    }
  ]
}
```

## Function Implementation

### Verify Functions

Verify functions should accept a `response` parameter and optional kwargs, returning a boolean:

```python
def verify_response_status_custom(response, expected_status=200):
    return response.status_code == expected_status

def verify_response_contains_text(response, expected_text="", case_sensitive=True):
    response_text = response.text
    if not case_sensitive:
        response_text = response_text.lower()
        expected_text = expected_text.lower()
    return expected_text in response_text
```

### Save Functions

Save functions should accept a `response` parameter and optional kwargs, returning a dictionary of variables:

```python
def extract_custom_data(response, field_path="", default_value="unknown"):
    try:
        data = response.json()
        current = data
        for field in field_path.split("."):
            current = current.get(field, {})
        return {
            f"extracted_{field_path.replace('.', '_')}": current if current != {} else default_value,
            "function_called": True
        }
    except Exception:
        return {
            f"extracted_{field_path.replace('.', '_')}": default_value,
            "function_called": True
        }

def extract_multiple_fields(response, fields=None):
    if fields is None:
        fields = ["slideshow.title", "slideshow.author"]
    
    result = {"function_called": True}
    try:
        data = response.json()
        for field_path in fields:
            current = data
            for field in field_path.split("."):
                current = current.get(field, {})
            result[f"extracted_{field_path.replace('.', '_')}"] = current if current != {} else "unknown"
    except Exception:
        for field_path in fields:
            result[f"extracted_{field_path.replace('.', '_')}"] = "unknown"
    
    return result
```

## Variable Substitution Rules

1. **Syntax**: Use `$variable_name` to reference variables
2. **Quoted strings**: Both `"$variable"` and `$variable` are supported
3. **Data types**: Variables can be strings, numbers, booleans, arrays, or objects
4. **Nested structures**: Variables work in nested objects and arrays
5. **Missing variables**: If a variable doesn't exist, the placeholder remains unchanged
6. **Same stage variables**: Variables saved in the same stage can be used in subsequent functions

## Benefits

1. **Reusability**: Functions can be parameterized and reused across different test scenarios
2. **Flexibility**: Different test cases can use the same function with different parameters
3. **Maintainability**: Centralized logic with configurable behavior
4. **Backward Compatibility**: Existing tests continue to work without modification
5. **Dynamic Configuration**: Use fixtures and saved variables to configure function behavior
6. **Reduced Duplication**: Parameterize functions instead of creating multiple similar functions

## Migration Guide

### From String Function Names

**Before:**
```json
{
  "functions": ["module:simple_function"]
}
```

**After (optional):**
```json
{
  "functions": [
    {
      "function": "module:simple_function",
      "kwargs": {}
    }
  ]
}
```

### Adding Parameters

**Before (required separate functions):**
```python
def verify_status_200(response):
    return response.status_code == 200

def verify_status_201(response):
    return response.status_code == 201
```

**After (single parameterized function):**
```python
def verify_status_custom(response, expected_status=200):
    return response.status_code == expected_status
```

```json
{
  "functions": [
    {
      "function": "module:verify_status_custom",
      "kwargs": {"expected_status": 200}
    },
    {
      "function": "module:verify_status_custom",
      "kwargs": {"expected_status": 201}
    }
  ]
}
```

### Using Variables

**Before (hardcoded values):**
```json
{
  "functions": [
    {
      "function": "module:verify_text",
      "kwargs": {
        "expected_text": "Sample Slide Show"
      }
    }
  ]
}
```

**After (parameterized with variables):**
```json
{
  "fixtures": ["expected_text"],
  "functions": [
    {
      "function": "module:verify_text",
      "kwargs": {
        "expected_text": "$expected_text"
      }
    }
  ]
}
```

## Error Handling

- If a function call fails, the test will fail with a descriptive error message
- Kwargs are passed as-is to the function - no validation is performed on the kwargs themselves
- Functions should handle their own parameter validation and provide meaningful error messages
- Variable substitution errors are caught and reported clearly

## Testing

The new functionality includes comprehensive tests:

- `tests/test_kwargs_integration.py`: Combined tests for models, substitution, and plugin functionality
- `tests/examples/test_kwargs_comprehensive.http.json`: Comprehensive example tests covering all kwargs functionality
- `tests/examples/test_kwargs_helpers.py`: Combined helper functions for all kwargs examples

## Optimized Test Structure

The test structure has been optimized to follow project rules:

1. **Combined Tests**: All kwargs-related tests are now in a single file with proper parametrization
2. **Shared Fixtures**: Common test data is shared through fixtures
3. **Functional Style**: Tests use functional programming style with minimal nesting
4. **No Docstrings**: Tests avoid unnecessary docstrings unless critical
5. **Parameterized Tests**: Similar test cases are combined using `@pytest.mark.parametrize`
6. **Comprehensive Examples**: All example scenarios are combined into a single comprehensive test file
7. **Unified Helpers**: All helper functions are combined into a single file for easier maintenance
