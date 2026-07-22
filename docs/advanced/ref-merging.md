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
2. **Arrays**: Concatenated — referenced elements first, then sibling elements. Arrays are *not* replaced and *not* merged element-by-element.
3. **Scalars**: A sibling must match the referenced value. Any **differing** scalar raises a merge conflict at load time (`Merge conflict at <path>`).
4. **Type mismatch**: Combining different JSON types at the same path (object vs array, scalar vs object, …) raises a merge conflict.

`null` is not an exception: it is a value like any other, not an override or a hole. A `null` paired with a different value at the same path is a merge conflict; two `null`s merge fine.

> **References add, they don't override.** To change a value a fragment already sets, don't merge over it — keep that key out of the shared fragment (so the local scenario is its only writer), or point the `$ref` at a sub-node that omits it. Trying to replace a referenced scalar with a different one is a load-time error by design, so a shared fragment can never be silently contradicted.

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

A fragment file may carry its own top-level `$schema` key for editor support — wherever the fragment lands in the referencing scenario, validation discards the key. Inline `verify.body.schema` values are the exception to resolution itself: that position holds a standard JSON Schema, so the resolver leaves the whole subtree untouched — its `$ref`/`$defs`/`$schema` belong to the schema validator, and scenario directives (`$include`/`$merge`, or a file-path `$ref`) are not processed there (the validator warns with `HTTPCHAIN028`). The opacity extends to sibling merging: two differing schema values arriving at the same position via `$merge` are a **merge conflict**, never blended — an opaque subtree merges atomically, like a scalar. To share a schema between scenarios, use the file-path form (`"schema": "./schemas/user.json"`) rather than a reference directive.

### Shared Configuration

**config/defaults.json:**
```json
{
    "ssl": {
        "verify": true
    },
    "auth": "auth_module:get_default_auth",
    "substitutions": [
        {
            "vars": {
                "base_url": "https://api.example.com",
                "timeout": 30
            }
        }
    ]
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
                "url": "{{ base_url }}/test",
                "timeout": "{{ timeout }}"
            }
        }
    ]
}
```

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
