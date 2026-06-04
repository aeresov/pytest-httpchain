---
name: pytest-httpchain
description: Write and edit pytest-httpchain HTTP API test scenarios in JSON format
---

# pytest-httpchain test authoring

pytest-httpchain is a pytest plugin for declarative HTTP API integration testing. Test scenarios are JSON files discovered by pattern `test_<name>.http.json`.

## Scenario structure

```json
{
  "description": "optional scenario description",
  "marks": ["optional_pytest_markers"],
  "substitutions": [],
  "stages": []
}
```

## Stage structure

```json
{
  "name": "stage name",
  "description": "optional",
  "fixtures": ["fixture_name"],
  "marks": ["skip", "xfail(reason='not ready')"],
  "always_run": false,
  "substitutions": [],
  "parametrize": [],
  "parallel": null,
  "request": { ... },
  "response": [ ... ]
}
```

Stages run sequentially and share a global context. Values saved in one stage are available in subsequent stages.

Stages can also be written as a dict (keys become stage names):

```json
{
  "stages": {
    "create user": { "request": { ... }, "response": [ ... ] },
    "get user":    { "request": { ... }, "response": [ ... ] }
  }
}
```

## Request

```json
{
  "url": "{{ server }}/api/users",
  "method": "POST",
  "headers": { "Authorization": "Bearer {{ token }}" },
  "params": { "page": 1 },
  "body": { "json": { "name": "Alice" } },
  "timeout": 30.0,
  "allow_redirects": true
}
```

**Body types** (use exactly one key):
- `{"json": { ... }}` - JSON body
- `{"form": { ... }}` - URL-encoded form
- `{"text": "..."}` - raw text
- `{"xml": "<root/>"}` - XML
- `{"base64": "..."}` - base64-encoded binary
- `{"binary": "/path/to/file"}` - file upload
- `{"files": {"field": "/path/to/file"}}` - multipart file upload
- `{"graphql": {"query": "...", "variables": {}}}` - GraphQL

## Response steps

Response is a list of verify and save steps, executed in order:

```json
"response": [
  {
    "verify": {
      "status": 200,
      "headers": { "content-type": "application/json" },
      "body": {
        "schema": { "type": "object", "required": ["id"] },
        "contains": ["expected text"],
        "not_contains": ["error"],
        "matches": ["\\d{4}-\\d{2}-\\d{2}"],
        "not_matches": ["forbidden"]
      }
    }
  },
  {
    "save": {
      "jmespath": {
        "user_id": "data.id",
        "user_name": "data.name",
        "total": "length(items)"
      }
    }
  },
  {
    "verify": {
      "expressions": [
        "{{ total > 0 }}",
        "{{ user_name != '' }}"
      ]
    }
  }
]
```

**Important:** `verify.expressions` are `{{ }}` templates evaluated against the **context** (saved variables, fixtures, substitutions). The HTTP response is **not** ambient in templates — there is no `response`/`status_code`/`body`/`json` variable. To assert on response data, either:
- use `verify.status`, `verify.headers`, `verify.body` (these check the response directly), or
- `save` the value first (e.g. via `jmespath`) and reference the saved variable in a later `expressions` step (as shown above).

**Save types:**
- `{"jmespath": {...}}` - extract values from JSON response via JMESPath
- `{"substitutions": [...]}` - compute values using template expressions
- `{"user_functions": [...]}` - call Python functions to process response

## Template expressions

Use `{{ expr }}` syntax. Expressions are evaluated with Python semantics.

**Available context:** all saved variables, fixture values, and substitution results.

**Built-in functions:** `len`, `min`, `max`, `sum`, `abs`, `round`, `sorted`, `range`, `zip`, `enumerate`, `bool`, `int`, `float`, `str`, `dict`, `list`, `tuple`, `set`, `uuid4()`, `env(var, default)`, `get(var, default)`, `exists(var)`, `rand()`, `randint(a, b)`

**JSON literals:** `true`, `false`, `null` map to Python `True`, `False`, `None`.

## Substitutions

Define variables before stages run:

```json
"substitutions": [
  { "vars": { "base_url": "https://api.example.com", "count": "{{ 2 + 3 }}" } },
  { "functions": { "generate_token": "mymodule:create_jwt" } }
]
```

Substitutions can appear at scenario level (global) or stage level (local).

## References ($include / $ref)

Split scenarios across files using `$include` (preferred) or `$ref`:

```json
{
  "request": {
    "$include": "common.json#/requests/get_user"
  }
}
```

Sibling properties are deep-merged with the referenced content:

```json
{
  "$include": "base_request.json",
  "headers": { "X-Custom": "override" }
}
```

## Parametrize

Run a stage with different inputs:

```json
"parametrize": [
  {
    "individual": { "user_id": [1, 2, 3] },
    "ids": ["user-one", "user-two", "user-three"]
  }
]
```

Or use combinations:

```json
"parametrize": [
  {
    "combinations": [
      { "method": "GET", "expected": 200 },
      { "method": "DELETE", "expected": 403 }
    ]
  }
]
```

## Parallel execution

Execute requests concurrently for load testing:

```json
"parallel": {
  "repeat": 100,
  "max_concurrency": 10,
  "calls_per_sec": 50
}
```

Or iterate over parameter sets in parallel:

```json
"parallel": {
  "foreach": [{ "individual": { "id": [1, 2, 3, 4, 5] } }],
  "max_concurrency": 5
}
```

## Complete example: multi-stage API test

```json
{
  "substitutions": [
    { "vars": { "base": "{{ env('API_URL', 'http://localhost:8000') }}" } }
  ],
  "stages": [
    {
      "name": "create user",
      "request": {
        "url": "{{ base }}/users",
        "method": "POST",
        "body": { "json": { "name": "Alice", "email": "alice@example.com" } }
      },
      "response": [
        { "verify": { "status": 201 } },
        { "save": { "jmespath": { "user_id": "id" } } }
      ]
    },
    {
      "name": "get user",
      "request": {
        "url": "{{ base }}/users/{{ user_id }}"
      },
      "response": [
        { "verify": { "status": 200 } },
        { "save": { "jmespath": { "name": "name" } } },
        { "verify": { "expressions": ["{{ name == 'Alice' }}"] } }
      ]
    },
    {
      "name": "delete user",
      "request": {
        "url": "{{ base }}/users/{{ user_id }}",
        "method": "DELETE"
      },
      "response": [
        { "verify": { "status": 204 } }
      ]
    }
  ]
}
```

## Validate your scenario

After writing a scenario, validate it (no server or network needed):

```bash
pytest-httpchain validate test_<name>.http.json
```

It checks structure plus semantics a JSON Schema cannot, each with a stable `HTTPCHAINxxx` code:

- `HTTPCHAIN003` — a `{{ var }}` that is never defined, saved, or provided as a fixture (likely a typo).
- `HTTPCHAIN004` — a variable used **before** the stage that saves it, or used in a stage's request when it is only saved in that same stage's response. Remember: a value `save`d in a stage's response is available to *later* response steps and *later* stages, never to the request that produced it.
- `HTTPCHAIN006` — a `verify` step that asserts nothing.
- `HTTPCHAIN007` / `HTTPCHAIN008` — body `contains`/`not_contains` (or `matches`/`not_matches`) that list the same value, which can never pass.

Add `--format json` for machine-readable output. The same checks run automatically during `pytest --collect-only`.

For a deeper check that imports your `module:func` references (confirming they resolve and their signatures match — including the injected `response` for save/verify functions) and verifies referenced files/schemas exist, add `--deep` (optionally `--syspath <dir>` for import roots, `--strict` to fail on warnings):

```bash
pytest-httpchain validate --deep test_<name>.http.json
```

Note: the HTTP response is **not** ambient in `{{ }}` templates — `save` what you need from a response first, then reference the saved variable.
