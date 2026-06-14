# Response Processing

Response processing is defined as a list of steps, each either a `verify` or `save` operation. Steps execute in order.

## Response Structure

```json
{
    "response": [
        {"verify": {...}},
        {"save": {...}},
        {"verify": {...}}
    ]
}
```

Or using dictionary format for organization:

```json
{
    "response": {
        "status_check": {"verify": {"status": 200}},
        "extract_data": {"save": {"jmespath": {"id": "data.id"}}},
        "validate_schema": {"verify": {"body": {"schema": {...}}}}
    }
}
```

## Verify Steps

### Status Code

```json
{
    "verify": {
        "status": 200
    }
}
```

Use template expressions:

```json
{
    "verify": {
        "status": "{{ expected_status }}"
    }
}
```

### Headers

```json
{
    "verify": {
        "headers": {
            "Content-Type": "application/json",
            "X-Request-Id": "{{ request_id }}"
        }
    }
}
```

### Expression Verification

Evaluate template expressions that must return truthy values. Expressions are evaluated against the **context** — saved variables, fixtures, and substitutions — **not** the raw HTTP response. Save the data you want to assert on first, then reference it:

```json
{
    "response": [
        {
            "save": {
                "jmespath": {
                    "items": "items",
                    "status": "status"
                }
            }
        },
        {
            "verify": {
                "expressions": [
                    "{{ len(items) > 0 }}",
                    "{{ status == 'ok' }}"
                ]
            }
        }
    ]
}
```

Response facets that aren't in the JSON body — elapsed time, raw text, individual headers — can't be reached with JMESPath. Capture them with a [save user function](#user-function-save), which receives the `httpx.Response`:

```python
# checks.py
import httpx

def response_meta(response: httpx.Response) -> dict:
    return {
        "response_time": response.elapsed.total_seconds(),
        "body_text": response.text,
    }
```

```json
{
    "response": [
        {"save": {"user_functions": ["checks:response_meta"]}},
        {
            "verify": {
                "expressions": [
                    "{{ response_time < 1.0 }}",
                    "{{ 'error' not in body_text }}"
                ]
            }
        }
    ]
}
```

`validate` can't see the keys a user function returns, so it reports `response_time` and `body_text` as `HTTPCHAIN003` ("potentially undefined"). The warning is expected here — the expressions resolve correctly at runtime once the save step has run.

### Body Content Checks

#### Contains / Not Contains

```json
{
    "verify": {
        "body": {
            "contains": ["success", "user_id"],
            "not_contains": ["error", "failed"]
        }
    }
}
```

#### Regex Matching

```json
{
    "verify": {
        "body": {
            "matches": ["\"id\":\\s*\\d+", "\"status\":\\s*\"ok\""],
            "not_matches": ["\"error\":", "\"failed\":"]
        }
    }
}
```

### JSON Schema Validation

Inline schema:

```json
{
    "verify": {
        "body": {
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "email": {"type": "string", "format": "email"}
                },
                "required": ["id", "name"]
            }
        }
    }
}
```

External schema file:

```json
{
    "verify": {
        "body": {
            "schema": "./schemas/user.json"
        }
    }
}
```

A relative schema path is **not** resolved against the scenario file's location.
It is resolved against the current working directory — the directory pytest was
launched from — so `"./schemas/user.json"` looks for `schemas/user.json` under
that directory, regardless of where the test file lives. To avoid surprises when
tests are run from a different directory, use an absolute path (for example one
built from a fixture) or run pytest from a consistent project root.

### User Function Verification

```json
{
    "verify": {
        "user_functions": [
            "mymodule:check_response",
            {
                "name": "mymodule:check_with_args",
                "kwargs": {
                    "expected_value": "{{ expected }}"
                }
            }
        ]
    }
}
```

```python
# mymodule.py
import httpx

def check_response(response: httpx.Response) -> bool:
    data = response.json()
    return data.get("status") == "ok"

def check_with_args(response: httpx.Response, expected_value: str) -> bool:
    return response.json().get("value") == expected_value
```

## Save Steps

### JMESPath Extraction

Extract values from JSON responses:

```json
{
    "save": {
        "jmespath": {
            "user_id": "data.user.id",
            "user_name": "data.user.name",
            "first_item": "items[0]",
            "all_ids": "items[*].id"
        }
    }
}
```

### Substitutions Save

Add computed values to context:

```json
{
    "save": {
        "substitutions": [
            {
                "vars": {
                    "timestamp": "{{ str(now_utc) }}"
                }
            },
            {
                "functions": {
                    "computed": "mymodule:compute_value"
                }
            }
        ]
    }
}
```

The template evaluator does not expose `datetime`, so provide values like timestamps via a fixture and reference the stage with `fixtures: ["now_utc"]`:

```python
# conftest.py
import pytest
from datetime import datetime

@pytest.fixture
def now_utc():
    return datetime.now()
```

### User Function Save

Extract data using custom functions:

```json
{
    "save": {
        "user_functions": [
            "mymodule:extract_data",
            {
                "name": "mymodule:extract_with_args",
                "kwargs": {
                    "key": "specific_field"
                }
            }
        ]
    }
}
```

```python
# mymodule.py
import httpx
from typing import Any

def extract_data(response: httpx.Response) -> dict[str, Any]:
    data = response.json()
    return {
        "user_id": data["user"]["id"],
        "token": response.headers.get("X-Auth-Token")
    }

def extract_with_args(response: httpx.Response, key: str) -> dict[str, Any]:
    return {key: response.json().get(key)}
```

## Complete Example

```json
{
    "stages": [
        {
            "name": "create_user",
            "request": {
                "url": "https://api.example.com/users",
                "method": "POST",
                "body": {
                    "json": {"name": "Test User", "email": "test@example.com"}
                }
            },
            "response": [
                {
                    "verify": {
                        "status": 201,
                        "headers": {
                            "Content-Type": "application/json"
                        }
                    }
                },
                {
                    "save": {
                        "jmespath": {
                            "user_id": "id",
                            "created_at": "created_at"
                        }
                    }
                },
                {
                    "verify": {
                        "body": {
                            "schema": {
                                "type": "object",
                                "required": ["id", "name", "email"]
                            }
                        }
                    }
                }
            ]
        },
        {
            "name": "verify_user",
            "request": {
                "url": "https://api.example.com/users/{{ user_id }}"
            },
            "response": [
                {
                    "verify": {
                        "status": 200,
                        "expressions": [
                            "{{ created_at is not none }}"
                        ]
                    }
                }
            ]
        }
    ]
}
```
