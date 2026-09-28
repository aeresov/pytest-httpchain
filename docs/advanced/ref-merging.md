# References and Deep Merging

pytest-httpchain supports JSON references for reusing scenario components across files. References are resolved with deep merging, allowing you to compose scenarios from shared fragments.

## `$include` / `$merge` vs `$ref`

Three directives are supported and work identically:

- **`$include`** (recommended): Avoids conflicts with VS Code's JSON Schema validation
- **`$merge`** (recommended): Alias for `$include`, semantically clearer when merging properties
- **`$ref`**: Standard JSON Reference syntax, but may cause VS Code/IDE validation warnings

```json
// Recommended - no VS Code conflicts
{ "$include": "common.json#/headers" }
{ "$merge": "base.json", "extra": "value" }

// Also works, but may show VS Code warnings
{ "$ref": "common.json#/headers" }
```

## Basic Syntax

Reference another file:

```json
{
    "$ref": "path/to/file.json"
}
```

Reference a specific key within a file:

```json
{
    "$ref": "path/to/file.json#/key/path"
}
```

## File References

### Same Directory

```json
{
    "$ref": "common.json"
}
```

### Relative Paths

```json
{
    "$ref": "../shared/auth.json"
}
```

### Nested Directories

```json
{
    "$ref": "fragments/requests/login.json"
}
```

### Lookup Order

A relative reference path is looked up against **two** bases, in order:

1. the **referencing file's directory** (the file containing the `$ref`);
2. the **root path** — pytest's rootdir when collecting, or `--root-path` /
   the auto-detected project root when using the CLI.

The first base under which the file exists wins. This lets a suite keep
fragments next to the scenarios that use them *and* reference shared
fragments by a root-relative path — but it also means the same string can
name two different files. When a file exists under **both** bases, the
file-relative one wins and an `AmbiguousReferenceWarning` is emitted
(reported as the `HTTPCHAIN026` diagnostic by `pytest-httpchain validate`),
because dropping a file next to a scenario silently changing what its
references mean is exactly the kind of surprise you want flagged. Rename one
of the files to resolve the ambiguity.

## JSON Pointer References

Reference specific keys using JSON Pointer syntax:

**common.json:**
```json
{
    "headers": {
        "default": {
            "Content-Type": "application/json",
            "Accept": "application/json"
        },
        "auth": {
            "Authorization": "Bearer {{ token }}"
        }
    },
    "requests": {
        "login": {
            "url": "https://api.example.com/login",
            "method": "POST"
        }
    }
}
```

**test_scenario.http.json:**
```json
{
    "stages": [
        {
            "name": "login",
            "request": {
                "$ref": "common.json#/requests/login",
                "headers": {
                    "$ref": "common.json#/headers/default"
                }
            }
        }
    ]
}
```

## Deep Merging

When a `$ref` is used alongside other properties, the siblings are deep merged *into* the referenced content — they **add** to it. A sibling cannot change a value the reference already sets; see [Merge Rules](#merge-rules).

**base.json:**
```json
{
    "request": {
        "url": "https://api.example.com/users",
        "headers": {
            "Content-Type": "application/json"
        },
        "timeout": 30
    }
}
```

**test_scenario.http.json:**
```json
{
    "stages": [
        {
            "name": "custom_request",
            "$ref": "base.json",
            "request": {
                "method": "POST",
                "headers": {
                    "X-Request-Id": "abc-123"
                }
            }
        }
    ]
}
```

The sibling `request` adds `method` and a new header. The nested `headers` object is merged recursively, so the referenced `Content-Type` is kept alongside the added `X-Request-Id`, and `url`/`timeout` carry through from `base.json` untouched.

**Resolved result:**
```json
{
    "stages": [
        {
            "name": "custom_request",
            "request": {
                "url": "https://api.example.com/users",
                "method": "POST",
                "headers": {
                    "Content-Type": "application/json",
                    "X-Request-Id": "abc-123"
                },
                "timeout": 30
            }
        }
    ]
}
```

## Merge Rules

A `$ref` (or `$include`/`$merge`) and its sibling properties are combined by **additive deep merge**: siblings extend the referenced value, they do not override it.

1. **Objects**: Recursively merged — sibling keys are added, and keys present in both are merged by these same rules.
2. **Arrays**: Concatenated — referenced elements first, then sibling elements. Arrays are *not* replaced and *not* merged element-by-element. A `verify.status` list is an exception, [below](#status-lists-merge-whole).
3. **Scalars**: A sibling must match the referenced value. Any **differing** scalar raises a merge conflict at load time (`Merge conflict at <path>`).
4. **Type mismatch**: Combining different JSON types at the same path (object vs array, scalar vs object, …) raises a merge conflict.

`null` is not an exception: it is a value like any other, not an override or a hole. A `null` paired with a different value at the same path is a merge conflict; two `null`s merge fine.

Equal means equal as JSON, at any depth: `true` and `1` differ, and so do `[true]` and `[1]`, while `1` and `1.0` are one value.

> **References add, they don't override.** To change a value a fragment already sets, don't merge over it — keep that key out of the shared fragment (so the local scenario is its only writer), or point the `$ref` at a sub-node that omits it. Trying to replace a referenced scalar with a different one is a load-time error by design, so a shared fragment can never be silently contradicted.

### Status lists merge whole

Elsewhere, concatenating arrays only adds — more steps, more `expressions` that must hold, more
`contains` strings — so a sibling can extend a fragment but never weaken it. A [`verify.status`](../usage/responses.md#status-code)
list is different: its entries are **alternatives**, any one of which passes, so a longer list
accepts more. Concatenating would let a sibling quietly loosen the fragment's check — a negative
test's `[404]` merged onto a shared `["2xx"]` would become `["2xx", 404]` and pass on a 200. A
`verify.status` list therefore merges like a scalar: an equal list is kept, and a different one is a
merge conflict:

**common.json:**
```json
{
    "ok": {"verify": {"status": ["2xx"]}}
}
```

```json
{"$merge": "common.json#/ok", "verify": {"status": [404]}}
```

```
Merge conflict at verify.status
```

To accept more codes, list them all in one place. A reference *inside* `status`
(`"status": {"$include": "codes.json#/accepted"}`) still resolves as usual.

### JMESPath expectations merge whole

What one [`verify.jmespath`](../usage/responses.md#jmespath-assertions)
expression must give is one value, so it merges like a scalar too. Concatenated,
a sibling's `["b"]` on a fragment's `["a"]` would assert `["a", "b"]`, which
neither wrote, and two objects under `eq` would blend into a third. An equal
expectation is kept and a different one, a matcher included, is a merge
conflict; different expressions still merge side by side:

**common.json:**
```json
{
    "checks": {"verify": {"jmespath": {"tags": ["a"], "price": {"gt": 0}, "meta": {"eq": {"page": 1}}}}}
}
```

```json
{"$merge": "common.json#/checks", "verify": {"jmespath": {"count": 3}}}
```

adds the `count` check to the fragment's three, while

```json
{"$merge": "common.json#/checks", "verify": {"jmespath": {"price": {"lt": 100}}}}
```

fails with `Merge conflict at verify.jmespath.price`: write the whole matcher
in one place, or build it from the shared one by writing the reference at the
expectation itself. There its siblings are one value composed on purpose, not
a second one written for the same expression, so they merge key by key:

```json
{"verify": {"jmespath": {"price": {"$merge": "common.json#/checks/verify/jmespath/price", "lt": 100}}}}
```

checks `{"gt": 0, "lt": 100}`. A key both sides give is one operand, and must
still agree whole, an array or an object as much as a number:

```json
{"verify": {"jmespath": {"meta": {"$merge": "common.json#/checks/verify/jmespath/meta", "eq": {"size": 2}}}}}
```

fails with `Merge conflict at eq` rather than checking `{"page": 1, "size": 2}`,
which neither side wrote, and two `contains` arrays conflict rather than
concatenate. To build an operand from a shared one, write the reference inside
it:

```json
{"verify": {"jmespath": {"meta": {"eq": {"$merge": "common.json#/checks/verify/jmespath/meta/eq", "size": 2}}}}}
```

checks `{"eq": {"page": 1, "size": 2}}`.

## Composing Scenarios

### Shared Stage Fragments

**fragments/stages.json:**
```json
{
    "login": {
        "name": "login",
        "request": {
            "url": "https://api.example.com/auth/login",
            "method": "POST",
            "body": {
                "json": {
                    "username": "{{ username }}",
                    "password": "{{ password }}"
                }
            }
        },
        "response": [
            {"verify": {"status": 200}},
            {"save": {"jmespath": {"token": "access_token"}}}
        ]
    },
    "logout": {
        "name": "logout",
        "always_run": true,
        "request": {
            "url": "https://api.example.com/auth/logout",
            "method": "POST",
            "headers": {
                "Authorization": "Bearer {{ token }}"
            }
        }
    }
}
```

**test_workflow.http.json:**
```json
{
    "substitutions": [
        {
            "vars": {
                "username": "testuser",
                "password": "testpass"
            }
        }
    ],
    "stages": [
        {
            "$ref": "fragments/stages.json#/login"
        },
        {
            "name": "do_something",
            "request": {
                "url": "https://api.example.com/action",
                "headers": {
                    "Authorization": "Bearer {{ token }}"
                }
            }
        },
        {
            "$ref": "fragments/stages.json#/logout"
        }
    ]
}
```

A fragment file may carry its own top-level `$schema` key for editor support — wherever the fragment lands in the referencing scenario, validation discards the key. Inline `verify.body.schema` values are the exception to resolution itself: that position holds a standard JSON Schema, so the resolver leaves the whole subtree untouched — its `$ref`/`$defs`/`$schema` belong to the schema validator, which resolves a `$ref` to a local file itself (relative to the scenario file's directory, unless an `$id` sets another base), and scenario directives (`$include`/`$merge`) are not processed there (the validator warns with `HTTPCHAIN028`). The opacity extends to sibling merging: two differing schema values arriving at the same position via `$merge` are a **merge conflict**, never blended — an opaque subtree merges atomically, like a scalar. To share a schema between scenarios, use the file-path form (`"schema": "./schemas/user.json"`, or `"./openapi.json#/components/schemas/User"` for one inside a document) or a JSON Schema `$ref`, rather than a reference directive.

### Shared Configuration

**config/defaults.json:**
```json
{
    "ssl": {
        "verify": true
    },
    "auth": {"bearer": "{{ env('API_TOKEN') }}"},
    "client": {
        "base_url": "https://api.example.com",
        "headers": {
            "Accept": "application/json"
        },
        "timeout": 30
    }
}
```

**test_with_defaults.http.json:**
```json
{
    "$ref": "config/defaults.json",
    "stages": [
        {
            "name": "test",
            "request": {
                "url": "/test"
            }
        }
    ]
}
```

The [`client` block](../usage/scenarios.md#client-configuration) gives every stage the base URL,
headers and timeout, so the stages spell out only their own path. A value a stage takes from a
reference counts as the stage's own: a request fragment `$include`d with `"timeout": 30` keeps 30
seconds whatever `client.timeout` says.

## Security: Path Traversal Limits

The `httpchain_ref_parent_traversal_depth` configuration limits how many `../` segments are allowed:

```ini
# pytest.ini
[pytest]
httpchain_ref_parent_traversal_depth = 3
```

With depth 3, these are valid:
- `../file.json`
- `../../file.json`
- `../../../file.json`

This would fail:
- `../../../../file.json`

### The root path is a containment boundary

The traversal depth is not the only constraint. Every resolved reference must
also stay **inside the root path** — the directory `root_path` names above. A
reference that resolves outside it is rejected even when the file exists and the
`../` count is within the limit; symlinks are resolved *before* the check, so a
link pointing out of the tree is rejected too. Absolute paths are refused
outright.

The root is pytest's `rootdir` during collection. For the CLI it is inferred
from the nearest ancestor holding a real pytest config, and `--root-path` sets
it explicitly:

```bash
pytest-httpchain validate --root-path . tests/test_login.http.json
```

A reference rejected this way says so specifically — `resolves outside the
reference root <dir>` — rather than reporting the file as missing.

The same three rules — relative paths only, the traversal depth, the root —
hold for a JSON Schema `$ref` to a file inside a `verify.body.schema` (see
[references between documents](../usage/responses.md#references-between-documents)).

## Circular Reference Detection

pytest-httpchain detects and prevents circular references:

**a.json:**
```json
{
    "$ref": "b.json"
}
```

**b.json:**
```json
{
    "$ref": "a.json"
}
```

This will raise an error during scenario loading.

## Best Practices

1. **Organize by purpose**: Group related fragments (auth, common headers, base configs)
2. **Use meaningful paths**: `fragments/auth/login.json` vs `f1.json`
3. **Keep references shallow**: Deeply nested refs are harder to debug
4. **Document shared files**: Add comments about expected variables
5. **Version shared fragments**: Consider separate directories for breaking changes
