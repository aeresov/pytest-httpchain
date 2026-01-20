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
| `always_run` | boolean | Execute even if prior stages failed |
| `substitutions` | array/object | Stage-specific variables |
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
-   `"usefixtures('fixture_name')"` - use fixtures

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
