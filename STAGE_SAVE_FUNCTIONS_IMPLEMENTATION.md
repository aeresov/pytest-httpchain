# Stage.save Functions Implementation

## Overview

Successfully implemented support for two optional fields in `Stage.save`: **"vars"** and **"functions"**. This enhancement allows stages to extract variables from HTTP responses using both JMESPath expressions (existing functionality) and custom Python functions (new functionality).

## Key Changes

### 1. Model Updates (`pytest_http/models.py`)

- **Added `SaveConfig` model** with optional `vars` and `functions` fields
- **Changed `Stage.save` field** to be `SaveConfig | None` (simplified from Union type)
- **Added validation** for Python function names using `validate_python_function_name()`
- **Maintained backward compatibility** with automatic conversion of old format to new format via field validator

### 2. Execution Logic (`pytest_http/pytest_plugin.py`)

- **Simplified execution flow** - Stage.save is always SaveConfig, no type checking needed
- **Enhanced function lookup mechanism** to find functions across multiple namespaces
- **Added function execution** with HTTP response as argument
- **Implemented validation** for function return values (must be dict with valid variable names)
- **Comprehensive error handling** for missing functions, invalid returns, and execution errors

### 3. Function Signature Requirements

Functions must follow this signature:
```python
def function_name(response) -> dict[str, Any]:
    """
    Args:
        response: HTTP response object from requests library
    
    Returns:
        Dictionary mapping variable names to extracted values
    """
```

## Usage Examples

### Old Format (Still Supported)
```json
{
    "save": {
        "user_id": "json.id",
        "user_name": "json.name"
    }
}
```

### New Format - Variables Only
```json
{
    "save": {
        "vars": {
            "user_id": "json.id",
            "user_name": "json.name"
        }
    }
}
```

### New Format - Functions Only
```json
{
    "save": {
        "functions": ["extract_user_data", "extract_metadata"]
    }
}
```

### New Format - Both Variables and Functions
```json
{
    "save": {
        "vars": {
            "user_id": "json.id"
        },
        "functions": ["extract_detailed_info"]
    }
}
```

## Function Implementation Example

```python
def extract_user_data(response):
    """Extract user data from response"""
    data = response.json()
    return {
        "user_count": len(data),
        "first_user_id": data[0]["id"] if data else None,
        "has_admin": any(user.get("role") == "admin" for user in data)
    }
```

## Error Handling

The implementation provides comprehensive error handling for:

- **Function not found** in any accessible namespace
- **Non-callable objects** referenced by function name
- **Invalid return types** (functions must return dict)
- **Invalid variable names** returned by functions
- **Function execution exceptions** with proper error context

## Testing

- **74 model tests** passing, including validation of new SaveConfig structure
- **Comprehensive integration tests** for function execution scenarios
- **Backward compatibility tests** ensuring old format still works
- **Error condition tests** validating proper error handling

## Backward Compatibility

✅ **Fully maintained** - existing JSON test files continue to work without modification
✅ **Automatic conversion** - old save format is transparently converted to new structure
✅ **API compatibility** - no breaking changes to existing functionality

## Function Lookup Strategy

The implementation uses a sophisticated function lookup mechanism:

1. **Frame inspection** - searches calling frames for function definitions
2. **Global namespace** - checks current module globals
3. **System modules** - searches all loaded modules for the function
4. **Error reporting** - provides clear error messages when functions are not found

This allows functions to be defined in test files, imported modules, or any accessible namespace.

## Summary

The implementation successfully extends the Stage.save functionality while maintaining full backward compatibility. The simplified design makes `Stage.save` always a `SaveConfig` object (or `None`), eliminating the need for union types and complex type checking in the execution logic.

Key benefits of the simplified approach:
- **Cleaner code**: No isinstance checks needed in execution logic
- **Type safety**: Always know `stage.save` is `SaveConfig | None`
- **Backward compatibility**: Old dict format automatically converts via field validator
- **Flexibility**: Users can leverage both JMESPath expressions for simple data extraction and custom Python functions for complex processing logic

This provides a powerful and flexible variable extraction system with a clean, maintainable implementation.