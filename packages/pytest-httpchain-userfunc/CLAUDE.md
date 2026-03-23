# pytest-httpchain-userfunc

User function import and invocation library for pytest-httpchain.

## Purpose

This package provides utilities for dynamically importing and calling user-defined functions from explicit module paths (`module.submodule:func`).

## Package Structure

```
src/pytest_httpchain_userfunc/
├── __init__.py          # Public API exports
├── userfunc.py          # Core implementation: import_function, call_function, wrap_function
└── exceptions.py        # UserFunctionError exception
```

## Public API

```python
from pytest_httpchain_userfunc import (
    NAME_PATTERN,
    import_function,
    call_function,
    wrap_function,
    UserFunctionError,
)

# Import a function by name
func = import_function("module.path:function_name")

# Import and call in one step
result = call_function("mymodule:my_func", arg1, arg2, kwarg=value)

# Create a wrapped callable with default arguments
wrapped = wrap_function("mymodule:my_func", default_args=[arg1], default_kwargs={"key": "value"})
result = wrapped(extra_arg)  # default_args prepended, default_kwargs merged
```

## Function Name Format

Functions are referenced using the pattern: `module.path:function_name`

- Module path is required: `mypackage.utils:my_function`
- Bare function names (without module) raise `UserFunctionError`

## Key Behaviors

### wrap_function Argument Merging
- `default_args` are prepended to call-time args
- `default_kwargs` are merged with call-time kwargs (call-time wins)

### Error Handling
All errors raise `UserFunctionError` with descriptive messages:
- Invalid function name format
- Module path is required (bare names not supported)
- Module import failures
- Function not found in module
- Target is not callable
- Runtime errors during function execution

## Running Tests

```bash
# From monorepo root
uv run pytest packages/pytest-httpchain-userfunc/tests -v

# Or from package directory
cd packages/pytest-httpchain-userfunc
uv run pytest tests -v
```
