# Scenarios and Stages

## Scenario Structure

A scenario is a JSON file that defines a complete test case. The basic structure:

```json
{
    "description": "Optional description of this scenario",
    "marks": [],
    "fixtures": [],
    "auth": null,
    "ssl": {},
    "substitutions": [],
    "stages": []
}
```

| Field | Type | Description |
|-------|------|-------------|
| `description` | string | Optional human-readable description |
| `marks` | array | pytest markers applied to all stages |
| `fixtures` | array | pytest fixtures available to all stages |
| `auth` | string/object | Default authentication for all requests |
| `ssl` | object | SSL/TLS configuration |
| `substitutions` | array/object | Variables and functions for the context |
| `stages` | array/object | The test stages to execute |

Field names are validated strictly at every level: an unknown or misspelled key (`"headerz"`, `"alwaysrun"`) fails validation at collection time, naming the key and its location. The only exceptions are the `$schema` editor key (discarded during validation) and the `$ref`/`$include`/`$merge` reference directives (resolved before validation).

## Stage Structure

Each stage represents a single HTTP request:

```json
{
    "name": "stage name",
    "description": "Optional description",
    "marks": [],
    "fixtures": [],
    "always_run": false,
    "substitutions": [],
    "parametrize": null,
    "parallel": null,
    "request": {},
    "response": []
}
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Stage identifier (used in test names) |
| `description` | string | Optional description |
| `marks` | array | pytest markers for this stage |
| `fixtures` | array | pytest fixtures for this stage |
| `always_run` | boolean or template | Execute even if prior stages failed |
| `substitutions` | array/object | Stage-specific variables |
| `parametrize` | array/object | Parametrization steps that expand the stage into multiple test cases (see [Parametrization](../advanced/parametrization.md)) |
| `parallel` | object | Parallel execution config (`repeat`/`foreach`) for running the request concurrently (see [Parallel Execution](../advanced/parallel.md)) |
| `request` | object | HTTP request configuration |
| `response` | array/object | Response processing steps |

## Stages as List vs Dictionary

Stages can be defined as a list or dictionary. With dictionary format, keys become stage names:

**List format:**

```json
{
    "stages": [
        {
            "name": "login",
            "request": {"url": "https://api.example.com/login"}
        },
        {
            "name": "get_data",
            "request": {"url": "https://api.example.com/data"}
        }
    ]
}
```

**Dictionary format:**

```json
{
    "stages": {
        "login": {
            "request": {"url": "https://api.example.com/login"}
        },
        "get_data": {
            "request": {"url": "https://api.example.com/data"}
        }
    }
}
```

## Multi-Stage Execution

Stages execute in order. If one fails, subsequent stages are skipped unless `always_run` is set:

```json
{
    "stages": [
        {
            "name": "setup",
            "request": {"url": "https://api.example.com/setup", "method": "POST"}
        },
        {
            "name": "test",
            "request": {"url": "https://api.example.com/test"}
        },
        {
            "name": "cleanup",
            "always_run": true,
            "request": {"url": "https://api.example.com/cleanup", "method": "DELETE"}
        }
    ]
}
```

The `cleanup` stage runs even if `setup` or `test` fails.

`always_run` also accepts a template expression, evaluated (with Python truthiness) at the moment a stage is about to be skipped after a failure. In scope are fixtures, parametrize parameters, scenario-level substitutions, and variables saved by earlier stages — but *not* the stage's own substitutions, which are only processed once the stage actually runs:

```json
{
    "stages": [
        {
            "name": "create",
            "request": {"url": "https://api.example.com/resource", "method": "POST"},
            "response": [{"save": {"jmespath": {"resource_id": "id"}}}]
        },
        {
            "name": "test",
            "request": {"url": "https://api.example.com/test"}
        },
        {
            "name": "cleanup",
            "always_run": "{{ exists('resource_id') }}",
            "request": {"url": "https://api.example.com/resource/{{ resource_id }}", "method": "DELETE"}
        }
    ]
}
```

After a failure, `cleanup` runs only if `create` got far enough to save `resource_id` — there is nothing to delete otherwise. A stage that fails discards its saves, which is why the example guards with `exists()` instead of referencing `resource_id` directly. The result is plain Python truthiness, so beware that a saved *string* `"false"` is truthy. The validator checks `always_run` references against this scope (`HTTPCHAIN003`/`HTTPCHAIN004`).

## Pytest Integration

### Markers

Apply pytest markers at scenario or stage level:

```json
{
    "marks": ["slow", "integration"],
    "stages": [
        {
            "name": "skipped_stage",
            "marks": ["skip(reason='not implemented')"],
            "request": {"url": "https://api.example.com"}
        }
    ]
}
```

Supported marker formats:

-   `"skip"` - simple marker
-   `"skip(reason='message')"` - marker with arguments
-   `"xfail"` - expected failure
-   `"usefixtures('fixture_name')"` - trigger a fixture's setup/teardown for the stage

!!! note
    `usefixtures` only runs the fixture (for its side effects/setup); it does **not**
    make the fixture's value available to `{{ }}` templates. To use a fixture value
    in templates, list it in the stage or scenario [`fixtures`](#fixtures) array.

### Fixtures

Fixtures are loaded into the common data context:

```python
# conftest.py
import pytest

@pytest.fixture
def api_token():
    return "secret-token-123"

@pytest.fixture
def base_url():
    return "https://api.example.com"
```

```json
{
    "fixtures": ["base_url"],
    "stages": [
        {
            "name": "authenticated_request",
            "fixtures": ["api_token"],
            "request": {
                "url": "{{ base_url }}/protected",
                "headers": {
                    "Authorization": "Bearer {{ api_token }}"
                }
            }
        }
    ]
}
```

Scenario-level fixtures are requested by every stage. A few consequences to keep in mind:

-   Fixture values take precedence over previously saved variables of the same name, so don't save under a scenario fixture's name (the validator warns with `HTTPCHAIN009`).
-   pytest scoping still applies: a function-scoped fixture is set up again for each stage. Use `class`- or `session`-scoped fixtures for state that must survive across stages.
-   Fixtures can be referenced in *stage* templates only. Scenario-level `substitutions`, `auth`, and `ssl` resolve once at collection time, before any fixture exists; the validator rejects such references (`HTTPCHAIN016`).
-   A fixture whose value is itself **callable** is treated as a factory: it is wrapped so `{{ my_fixture(...) }}` invokes it (with context-manager fixtures entered on use and cleaned up afterwards). The wrapper is a different object than the original callable, so attribute access on it (`{{ my_fixture.some_attr }}`) is not available — call it instead.

## SSL Configuration

Configure SSL/TLS at the scenario level:

```json
{
    "ssl": {
        "verify": true,
        "cert": null
    },
    "stages": [...]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `verify` | bool/string | `true` (verify), `false` (skip), or path to CA bundle |
| `cert` | string/array | Client certificate path, or `[cert_path, key_path]` |

**Disable verification (development only):**

```json
{
    "ssl": {"verify": false}
}
```

**Custom CA bundle:**

```json
{
    "ssl": {"verify": "/path/to/ca-bundle.crt"}
}
```

**Client certificate:**

```json
{
    "ssl": {
        "cert": ["/path/to/client.crt", "/path/to/client.key"]
    }
}
```
