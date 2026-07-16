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

A **string** value is matched by exact, full-string equality — not substring. A
response with `Content-Type: application/json; charset=utf-8` does **not** match
`"application/json"`.

```json
{
    "verify": {
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "X-Request-Id": "{{ request_id }}"
        }
    }
}
```

For partial or pattern matches, use a **matcher object** instead of a string.
Any combination of `contains`, `not_contains`, `matches`, `not_matches`
(regexes, checked with `re.search`) is allowed; at least one is required:

```json
{
    "verify": {
        "headers": {
            "Content-Type": {"contains": "application/json"},
            "X-Request-Id": {"matches": "^[0-9a-f-]+$"},
            "Warning": {"not_contains": "deprecated"}
        }
    }
}
```

An **absent** header behaves as an empty string for matcher forms — `contains`
and `matches` fail, `not_contains` and `not_matches` pass vacuously. (Exact
string form fails for an absent header, as before.)

### Expression Verification

Evaluate template expressions that must return truthy values. Expressions are evaluated against the **context** — saved variables, fixtures, and substitutions — plus the reserved **`response` metadata namespace** (see below). For response *body* data, save what you want to assert on first, then reference it:

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

#### The `response` Namespace

Every **response step** (save and verify alike) sees a reserved `response`
namespace holding the response's metadata:

| Name | Type | Meaning |
|------|------|---------|
| `response.status` | int | HTTP status code |
| `response.reason` | str | Reason phrase (`"OK"`, `"Not Found"`) |
| `response.headers` | mapping | Response headers, case-insensitive keys |
| `response.elapsed_ms` | float | Round-trip time in milliseconds |

Use it directly in verify expressions:

```json
{
    "verify": {
        "expressions": [
            "{{ response.status == 200 }}",
            "{{ 'json' in response.headers['content-type'] }}",
            "{{ response.elapsed_ms < 500 }}"
        ]
    }
}
```

Or save a header for later stages with a substitutions save:

```json
{
    "save": {
        "substitutions": [
            {"vars": {"request_id": "{{ response.headers['x-request-id'] }}"}}
        ]
    }
}
```

The name `response` is reserved inside response steps: a variable, save, or
fixture with that name is shadowed there (the validator warns with
`HTTPCHAIN027`). It is only in scope in response steps — referencing it in a
request template is an error. The response **body** is deliberately not in the
namespace; extract body data with a `save` step.

Response facets beyond the metadata — e.g. the raw body text — can be captured with a [save user function](#user-function-save), which receives the `httpx.Response`:

```python
# checks.py
import httpx

def body_meta(response: httpx.Response) -> dict:
    return {
        "body_text": response.text,
    }
```

```json
{
    "response": [
        {"save": {"user_functions": ["checks:body_meta"]}},
        {
            "verify": {
                "expressions": [
                    "{{ 'error' not in body_text }}"
                ]
            }
        }
    ]
}
```

`validate` can't see the keys a user function returns, so it reports `body_text` as `HTTPCHAIN003` ("potentially undefined"). The warning is expected here — the expression resolves correctly at runtime once the save step has run.

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

A relative schema path is resolved against the **scenario file's directory** —
the same rule as `$ref`/`$include` — so `"./schemas/user.json"` looks for
`schemas/user.json` next to the test file, regardless of where pytest was
launched from. The same rule applies to every file path in the dialect:
`body.binary`, `body.files` values, and `ssl.cert`/`ssl.verify`. Absolute paths
pass through unchanged.

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
                            "Content-Type": "application/json; charset=utf-8"
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
                            "{{ created_at is not None }}"
                        ]
                    }
                }
            ]
        }
    ]
}
```
