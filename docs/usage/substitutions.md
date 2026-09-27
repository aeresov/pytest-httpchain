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

Templates read an object in `vars` with attribute access
(`{{ object_var.nested }}`). Where a field takes a whole object, one template
naming it stands for that object: a JSON body
(`"json": "{{ object_var }}"`), GraphQL `variables`, `verify.body.schema`, a
header matcher, and the `combinations` of a `parametrize` or `parallel.foreach`
step.

## Function Substitutions

Bind a name to a Python function so it can be **called** from templates:

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

A function substitution seeds a **callable** under its alias — it is not invoked
when the substitution is processed. Call it with `()` in a template to get a
value; referencing it bare renders the function object itself:

```json
{
    "headers": {
        "X-Request-Id": "{{ uuid() }}",
        "X-Timestamp": "{{ timestamp() }}"
    },
    "body": {
        "json": {"environment": "{{ config()['environment'] }}"}
    }
}
```

Each call is re-evaluated per use (a fresh `uuid()` every time). A function that
returns a dict is accessed with subscript (`config()['environment']`).

## Template Expressions

Use `{{ expression }}` syntax in any request value. Expressions support Python syntax via simpleeval.

!!! note
    Only **values** are substituted. A template in a dict *key* — a header name,
    a query parameter name, a JSON body key — is never rendered and is sent
    literally; the validator reports it as `HTTPCHAIN029`. Move the dynamic part
    into the value, or build the object in a user function.

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
-   `sorted()`
-   `enumerate()`, `zip()`
-   `uuid4()`, `rand()`, `randint(top)`
-   `env(var, default)`
-   `get(var, default)`, `exists(var)`

### List/Dict Comprehensions

When `items` comes from scenario `vars`, each element is a namespace, so use
attribute access (`item.id`). Subscript (`item['id']`) is for plain dicts coming
from fixtures or `combinations` parameters.

```json
{
    "body": {
        "json": {
            "ids": "{{ [item.id for item in items] }}",
            "names": "{{ {item.id: item.name for item in items} }}"
        }
    }
}
```

Note: Comprehension length is limited by `httpchain_max_comprehension_length` config.

### Templates that render to `null`

A template can render to `null`: `get()` without a default, or a JMESPath save
of a key the response did not have. On an optional setting or check, `null`
reads as "not declared", so instead of quietly switching it off the stage
fails, naming the field and the template as written:

```
'verify.headers.Content-Type.contains' was declared as '{{ expected_ct }}' but rendered to None, which would silently disable it
```

This covers every optional field that takes a template: `verify.status`,
`verify.body.schema`, the header matcher fields (`contains`, `not_contains`,
`matches`, `not_matches`), `parallel.calls_per_sec`, `request.auth`, and
scenario-level `ssl.cert` (which fails scenario initialization: the first stage
fails, and every later stage skips). A header matcher written as one template
(`"Content-Type": "{{ matcher }}"`) is covered too: a key the rendered matcher
sets to `null` fails, naming the field and that template, while a key it leaves
out is simply not checked.

A required field such as `url` or `timeout`, and a header matcher whose only
field rendered to `null`, cannot be switched off, but they fail with the same
message, naming the field and the template; it just ends at "rendered to None".
An exact-match header string is a value in the `headers` map rather than a
field, and fails validation when its template renders to `null`.

If something else in the same request, verify step or setting is invalid too,
pydantic's report on it follows the message, so the `null` is never blamed for
an error it did not cause.

Where `null` is itself a value, it is passed on as one:

-   a JSON body (`"json": "{{ payload }}"`) sends the JSON document `null`;
-   a query parameter or form field is sent with an empty value (`?q=`);
-   an auth function kwarg or a substitution variable receives `None`;
-   `always_run` is evaluated for truthiness, so `null` means "do not run".

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
