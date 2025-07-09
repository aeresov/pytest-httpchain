# Kwargs Functionality for Verify and Save Functions

This document describes the new kwargs functionality that allows user functions in "verify" and "save" blocks to be called with arbitrary keyword arguments.

## Overview

Previously, user functions in verify and save blocks could only be called with a single `response` argument. Now, you can optionally pass arbitrary keyword arguments to these functions, making them more flexible and reusable.

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
            "function": "test_verify_helpers:verify_response_status_custom",
            "kwargs": {
              "expected_status": 200
            }
          },
          {
            "function": "test_verify_helpers:verify_response_contains_text",
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
            "function": "test_helpers:extract_custom_data",
            "kwargs": {
              "field_path": "slideshow.author",
              "default_value": "unknown_author"
            }
          },
          {
            "function": "test_helpers:extract_multiple_fields",
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
    """Verify that the response status matches the expected status."""
    return response.status_code == expected_status

def verify_response_contains_text(response, expected_text="", case_sensitive=True):
    """Verify that the response text contains the expected text."""
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
    """Extract custom data from a specific field path."""
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
    """Extract multiple fields from the response."""
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

## Benefits

1. **Reusability**: Functions can be parameterized and reused across different test scenarios
2. **Flexibility**: Different test cases can use the same function with different parameters
3. **Maintainability**: Centralized logic with configurable behavior
4. **Backward Compatibility**: Existing tests continue to work without modification

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

## Error Handling

- If a function call fails, the test will fail with a descriptive error message
- Kwargs are passed as-is to the function - no validation is performed on the kwargs themselves
- Functions should handle their own parameter validation and provide meaningful error messages

## Testing

The new functionality includes comprehensive tests:

- `tests/test_kwargs_functionality.py`: Tests for the new models and validation
- `tests/test_plugin_kwargs_functionality.py`: Tests for the plugin's handling of kwargs
- `tests/examples/test_verify_functions_with_kwargs.http.json`: Example tests for verify functions
- `tests/examples/test_functions_with_kwargs.http.json`: Example tests for save functions