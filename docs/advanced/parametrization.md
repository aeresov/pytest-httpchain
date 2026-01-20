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
                "url": "https://api.example.com/users/{{ user['id'] }}",
                "headers": {
                    "X-User-Name": "{{ user['name'] }}"
                }
            }
        }
    ]
}
```

## Complete Example

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
            "name": "test_crud_operations",
            "parametrize": [
                {
                    "combinations": [
                        {
                            "method": "GET",
                            "path": "/users",
                            "expected_status": 200,
                            "body": null
                        },
                        {
                            "method": "POST",
                            "path": "/users",
                            "expected_status": 201,
                            "body": {"name": "New User"}
                        },
                        {
                            "method": "GET",
                            "path": "/users/999",
                            "expected_status": 404,
                            "body": null
                        }
                    ],
                    "ids": ["list_users", "create_user", "user_not_found"]
                }
            ],
            "request": {
                "url": "{{ base_url }}{{ path }}",
                "method": "{{ method }}",
                "body": "{{ {'json': body} if body else None }}"
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
