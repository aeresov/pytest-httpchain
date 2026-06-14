# pytest-httpchain-userfunc

Dynamic import and invocation of user-defined functions for
[pytest-httpchain](https://github.com/aeresov/pytest-httpchain).

This package resolves a function from an explicit `module.path:function_name`
reference and calls it. A bare name with no module path is rejected. It exposes
`import_function` (resolve only), `call_function` (resolve and call), and
`wrap_function` (build a callable with default args/kwargs that are merged with
call-time arguments). Import and runtime failures are raised as
`UserFunctionError` with the underlying cause appended to the message, so the
reason is visible even where only the message text is rendered.

## Role in the workspace

pytest-httpchain lets a scenario hook into user Python for custom
authentication, response verification, data extraction, and substitution
functions, each referenced as `module:function`. The plugin uses this package
to import and invoke those references at the right point in a stage's lifecycle.
It is published separately so the import/invocation logic can be reused and
tested on its own.

## Usage

```python
from pytest_httpchain_userfunc import call_function, wrap_function, UserFunctionError

try:
    result = call_function("mypackage.checks:status_ok", response)

    wrapped = wrap_function("mypackage.checks:equals", default_kwargs={"expected": 200})
    wrapped(response)  # default_kwargs merged with call-time kwargs
except UserFunctionError as e:
    print(f"user function failed: {e}")
```

## Links

- Documentation: <https://aeresov.github.io/pytest-httpchain/>
- Source and issues: <https://github.com/aeresov/pytest-httpchain>
