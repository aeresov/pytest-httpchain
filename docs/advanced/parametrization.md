# Parametrization

Stage parametrization allows running a stage multiple times with different values, similar to pytest's `@pytest.mark.parametrize`.

## Individual Parameters

Run a stage with different values for a single parameter:

```json
{
    "stages": [
        {
            "name": "test_users",
            "parametrize": [
                {
                    "individual": {
                        "user_id": [1, 2, 3, 4, 5]
                    }
                }
            ],
            "request": {
                "url": "https://api.example.com/users/{{ user_id }}"
            },
            "response": [
                {"verify": {"status": 200}}
            ]
        }
    ]
}
```

This creates 5 test runs, one for each user ID.

### With Custom IDs

```json
{
    "parametrize": [
        {
            "individual": {
                "status_code": [200, 404, 500]
            },
            "ids": ["success", "not_found", "server_error"]
        }
    ]
}
```

## Combinations

Run a stage with multiple parameter combinations:

```json
{
    "stages": [
        {
            "name": "test_endpoints",
            "parametrize": [
                {
                    "combinations": [
                        {"method": "GET", "endpoint": "/users"},
                        {"method": "GET", "endpoint": "/posts"},
                        {"method": "POST", "endpoint": "/users"},
                        {"method": "DELETE", "endpoint": "/users/1"}
                    ]
                }
            ],
            "request": {
                "url": "https://api.example.com{{ endpoint }}",
                "method": "{{ method }}"
            }
        }
    ]
}
```

### With Custom IDs

```json
{
    "parametrize": [
        {
            "combinations": [
                {"role": "admin", "can_delete": true},
                {"role": "user", "can_delete": false},
                {"role": "guest", "can_delete": false}
            ],
            "ids": ["admin_perms", "user_perms", "guest_perms"]
        }
    ]
}
```

## Multiple Parameter Sets

Chain multiple parameter definitions:

```json
{
    "parametrize": [
        {
            "individual": {
                "env": ["dev", "staging", "prod"]
            }
        },
        {
            "individual": {
                "format": ["json", "xml"]
            }
        }
    ]
}
```

This creates a cross-product: 3 environments × 2 formats = 6 test runs.

## Template Expressions in Parameters

Parameter values can use template expressions:

```json
{
    "substitutions": [
        {
            "vars": {
                "test_users": [
                    {"id": 1, "name": "Alice"},
                    {"id": 2, "name": "Bob"}
                ]
            }
        }
    ],
    "stages": [
        {
            "name": "dynamic_params",
            "parametrize": [
                {
                    "individual": {
                        "user": "{{ test_users }}"
                    }
                }
            ],
            "request": {
                "url": "https://api.example.com/users/{{ user.id }}",
                "headers": {
                    "X-User-Name": "{{ user.name }}"
                }
            }
        }
    ]
}
```

Values that come from scenario `vars` are exposed as namespaces, so use attribute
access (`{{ user.id }}`), not subscript (`{{ user['id'] }}`). Plain dicts from
fixtures or `combinations` parameters keep subscript access.

## Complete Example

A request body is a structured, typed value (a JSON body, a form body, …), not a plain string — so template expressions go **inside** a body type, such as `{"json": "{{ payload }}"}`, never as the whole `body` value. Because the body type is fixed by structure, a stage that sends a body and one that doesn't are written as separate stages.

This scenario combines scenario-level substitutions, two parametrized stages (a body-less read and a templated-body write), custom ids, and templated `url`/`status`:

```json
{
    "substitutions": [
        {
            "vars": {
                "base_url": "https://api.example.com"
            }
        }
    ],
    "stages": [
        {
            "name": "read_operations",
            "parametrize": [
                {
                    "combinations": [
                        {"path": "/users", "expected_status": 200},
                        {"path": "/users/999", "expected_status": 404}
                    ],
                    "ids": ["list_users", "user_not_found"]
                }
            ],
            "request": {
                "url": "{{ base_url }}{{ path }}",
                "method": "GET"
            },
            "response": [
                {
                    "verify": {
                        "status": "{{ expected_status }}"
                    }
                }
            ]
        },
        {
            "name": "create_user",
            "parametrize": [
                {
                    "combinations": [
                        {"payload": {"name": "Alice"}, "expected_status": 201},
                        {"payload": {"name": "Bob"}, "expected_status": 201}
                    ],
                    "ids": ["create_alice", "create_bob"]
                }
            ],
            "request": {
                "url": "{{ base_url }}/users",
                "method": "POST",
                "body": {"json": "{{ payload }}"}
            },
            "response": [
                {
                    "verify": {
                        "status": "{{ expected_status }}"
                    }
                }
            ]
        }
    ]
}
```
