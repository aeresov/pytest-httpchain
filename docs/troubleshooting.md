# Troubleshooting

## Test Files Not Discovered

Ensure your test files follow the naming pattern `test_<name>.<suffix>.json` where `suffix` defaults to `http`.

**Checklist:**

-   File name starts with `test_`
-   File name contains the suffix (default: `.http.`)
-   File extension is `.json`
-   Check `pytest.ini` if you've customized the suffix

**Example valid names:**

-   `test_api.http.json` (default suffix)
-   `test_users.http.json`
-   `test_auth.api.json` (if suffix configured as `api`)

## $ref Resolution Fails

### Path Issues

-   Verify the referenced file path is correct (relative to the referencing file)
-   Check that parent directory traversal doesn't exceed `ref_parent_traversal_depth` (default: 3)
-   Use forward slashes `/` even on Windows

### JSON Pointer Issues

-   Ensure the JSON pointer (e.g., `#/path/to/key`) points to an existing key
-   Keys are case-sensitive
-   Array indices are zero-based: `#/stages/0` for first stage

**Example:**

```json
{
    "$ref": "common/auth.json#/login_stage"
}
```

This references the `login_stage` key in `common/auth.json` relative to the current file.

## Template Expression Errors

### Variable Not Found

-   Ensure variables are defined in `substitutions` before use
-   Check that fixtures are listed in the `fixtures` array
-   Variables from `save` steps are only available in subsequent stages

### Syntax Errors

-   Template expressions use Python syntax inside `{{ }}`
-   Check for typos in variable names
-   Ensure quotes are balanced

**Valid expressions:**

```json
"{{ user_id }}"
"{{ user_id + 1 }}"
"{{ str(timestamp) }}"
"{{ 'prefix_' + name }}"
```

### Comprehension Limits

If you hit `MAX_COMPREHENSION_LENGTH` errors, either:

-   Simplify your expression
-   Increase `max_comprehension_length` in pytest config

## Stage Execution Stops Unexpectedly

### Chain Behavior

One stage failure stops the entire chain by default. This is intentional to prevent cascading failures.

**Solutions:**

-   Use `always_run: true` for cleanup stages that must execute regardless of prior failures
-   Check test output for specific error messages from failed verifications

### Verification Failures

Common causes:

-   Status code mismatch
-   Missing or incorrect response headers
-   JMESPath expression returns `null` instead of expected value
-   JSON Schema validation failure

## HTTP Request Errors

### Connection Errors

-   Verify the target server is running
-   Check URL for typos
-   Ensure network connectivity

### SSL/TLS Errors

Use SSL configuration to handle certificate issues:

```json
{
    "ssl": {
        "verify": false
    },
    "stages": [...]
}
```

Or specify a custom CA bundle:

```json
{
    "ssl": {
        "verify": "/path/to/ca-bundle.crt"
    },
    "stages": [...]
}
```

### Timeout Errors

Increase the timeout for slow endpoints:

```json
{
    "request": {
        "url": "https://slow-api.example.com/endpoint",
        "timeout": 60.0
    }
}
```

## User Function Errors

### Import Failures

-   Verify the module path uses dot notation: `mypackage.module:function`
-   Ensure the module is importable (in `PYTHONPATH` or installed)
-   Check for syntax errors in the module

### Function Signature Errors

Save functions must accept `httpx.Response` and return `dict[str, Any]`:

```python
def my_save(response: httpx.Response) -> dict[str, Any]:
    return {"key": response.json()["value"]}
```

Verify functions must accept `httpx.Response` and return `bool`:

```python
def my_verify(response: httpx.Response) -> bool:
    return response.json()["status"] == "ok"
```
