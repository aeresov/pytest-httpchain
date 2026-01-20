# $ref and Deep Merging

pytest-httpchain supports JSON Reference (`$ref`) for reusing scenario components across files. References are resolved with deep merging, allowing you to compose scenarios from shared fragments.

## Basic $ref Syntax

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

When a `$ref` is used alongside other properties, values are deep merged:

**base.json:**
```json
{
    "request": {
        "url": "https://api.example.com",
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
                "url": "https://api.example.com/custom",
                "headers": {
                    "X-Custom": "value"
                }
            }
        }
    ]
}
```

**Resolved result:**
```json
{
    "stages": [
        {
            "name": "custom_request",
            "request": {
                "url": "https://api.example.com/custom",
                "headers": {
                    "Content-Type": "application/json",
                    "X-Custom": "value"
                },
                "timeout": 30
            }
        }
    ]
}
```

## Merge Rules

1. **Objects**: Recursively merged (properties combined)
2. **Arrays**: Replaced entirely (no element merging)
3. **Scalars**: Local value overrides referenced value
4. **Type mismatch**: Local value wins

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

The `ref_parent_traversal_depth` configuration limits how many `../` segments are allowed:

```ini
# pytest.ini
[pytest]
ref_parent_traversal_depth = 3
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
