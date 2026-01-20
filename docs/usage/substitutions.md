# Substitutions and Templates

## Common Data Context

pytest-httpchain maintains a key-value context throughout scenario execution. This context is populated by:

1. Variables from `substitutions`
2. pytest fixtures
3. Values saved from responses

## Substitutions Structure

Substitutions can be defined at scenario or stage level:

```json
{
    "substitutions": [
        {
            "vars": {
                "base_url": "https://api.example.com",
                "timeout": 30
            }
        },
        {
            "functions": {
                "timestamp": "mymodule:get_timestamp"
            }
        }
    ]
}
```

### List vs Dictionary Format

**List format:**

```json
{
    "substitutions": [
        {"vars": {"key1": "value1"}},
        {"vars": {"key2": "value2"}}
    ]
}
```

**Dictionary format** (keys are organizational only):

```json
{
    "substitutions": {
        "config": {
            "vars": {
                "base_url": "https://api.example.com",
                "api_version": "v2"
            }
        },
        "auth": {
            "functions": {
                "token": "auth_module:get_token"
            }
        }
    }
}
```

**Mixed format:**

```json
{
    "substitutions": {
        "batch1": [
            {"vars": {"key1": "value1"}},
            {"vars": {"key2": "value2"}}
        ],
        "batch2": {"vars": {"key3": "value3"}}
    }
}
```

## Variable Substitutions

Define static values:

```json
{
    "substitutions": [
        {
            "vars": {
                "string_var": "hello",
                "number_var": 42,
                "bool_var": true,
                "list_var": [1, 2, 3],
                "object_var": {"nested": "value"}
            }
        }
    ]
}
```

## Function Substitutions

Call Python functions to compute values:

```json
{
    "substitutions": [
        {
            "functions": {
                "uuid": "uuid:uuid4",
                "timestamp": "mymodule:get_timestamp",
                "config": "mymodule:load_config"
            }
        }
    ]
}
```

```python
# mymodule.py
from datetime import datetime

def get_timestamp() -> str:
    return datetime.now().isoformat()

def load_config() -> dict:
    return {"environment": "test", "debug": True}
```

## Template Expressions

Use `{{ expression }}` syntax anywhere in requests. Expressions support Python syntax via simpleeval.

### Basic Variable Access

```json
{
    "url": "{{ base_url }}/users/{{ user_id }}"
}
```

### String Operations

```json
{
    "headers": {
        "Authorization": "{{ 'Bearer ' + token }}",
        "X-Request-ID": "{{ prefix + '_' + str(counter) }}"
    }
}
```

### Arithmetic

```json
{
    "params": {
        "offset": "{{ page * page_size }}",
        "limit": "{{ page_size }}"
    }
}
```

### Conditionals

```json
{
    "headers": {
        "X-Debug": "{{ 'true' if debug_mode else 'false' }}"
    }
}
```

### Built-in Functions

Available functions in expressions:

-   `str()`, `int()`, `float()`, `bool()`
-   `len()`, `range()`
-   `list()`, `dict()`, `set()`, `tuple()`
-   `min()`, `max()`, `sum()`
-   `abs()`, `round()`
-   `sorted()`, `reversed()`
-   `any()`, `all()`
-   `enumerate()`, `zip()`

### List/Dict Comprehensions

```json
{
    "body": {
        "json": {
            "ids": "{{ [item['id'] for item in items] }}",
            "names": "{{ {item['id']: item['name'] for item in items} }}"
        }
    }
}
```

Note: Comprehension length is limited by `max_comprehension_length` config.

## Stage-Level Substitutions

Override or add variables for specific stages:

```json
{
    "substitutions": [
        {"vars": {"base_url": "https://api.example.com"}}
    ],
    "stages": [
        {
            "name": "production_test",
            "substitutions": [
                {"vars": {"base_url": "https://prod.example.com"}}
            ],
            "request": {
                "url": "{{ base_url }}/health"
            }
        }
    ]
}
```

## Using Saved Values

Values saved from responses are available in subsequent stages:

```json
{
    "stages": [
        {
            "name": "login",
            "request": {
                "url": "https://api.example.com/login",
                "method": "POST",
                "body": {"json": {"user": "test", "pass": "secret"}}
            },
            "response": [
                {
                    "save": {
                        "jmespath": {
                            "auth_token": "token",
                            "user_id": "user.id"
                        }
                    }
                }
            ]
        },
        {
            "name": "get_profile",
            "request": {
                "url": "https://api.example.com/users/{{ user_id }}",
                "headers": {
                    "Authorization": "Bearer {{ auth_token }}"
                }
            }
        }
    ]
}
```

## Fixtures in Context

Pytest fixtures are added to context when listed:

```python
# conftest.py
import pytest
import os

@pytest.fixture
def api_key():
    return os.environ.get("API_KEY", "test-key")

@pytest.fixture
def test_user():
    return {"id": 1, "name": "Test User"}
```

```json
{
    "fixtures": ["api_key", "test_user"],
    "stages": [
        {
            "name": "authenticated_request",
            "request": {
                "url": "https://api.example.com/data",
                "headers": {
                    "X-API-Key": "{{ api_key }}"
                },
                "body": {
                    "json": {
                        "user_id": "{{ test_user['id'] }}",
                        "user_name": "{{ test_user['name'] }}"
                    }
                }
            }
        }
    ]
}
```
