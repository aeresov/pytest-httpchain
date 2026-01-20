# HTTP Requests

## Request Structure

```json
{
    "request": {
        "url": "https://api.example.com/endpoint",
        "method": "GET",
        "params": {},
        "headers": {},
        "body": null,
        "auth": null,
        "timeout": 30.0,
        "allow_redirects": true
    }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `url` | string | required | Target URL (supports templates) |
| `method` | string | `GET` | HTTP method |
| `params` | object | `{}` | Query parameters |
| `headers` | object | `{}` | Request headers |
| `body` | object | `null` | Request body configuration |
| `auth` | string/object | `null` | Authentication (overrides scenario-level) |
| `timeout` | number | `30.0` | Request timeout in seconds |
| `allow_redirects` | boolean | `true` | Follow redirects |

## URL and Query Parameters

```json
{
    "request": {
        "url": "https://api.example.com/users/{{ user_id }}",
        "params": {
            "page": 1,
            "limit": "{{ page_size }}",
            "filter": "active"
        }
    }
}
```

## Headers

```json
{
    "request": {
        "url": "https://api.example.com/data",
        "headers": {
            "Authorization": "Bearer {{ token }}",
            "Content-Type": "application/json",
            "X-Request-ID": "{{ request_id }}"
        }
    }
}
```

## Request Body Types

### JSON Body

```json
{
    "request": {
        "url": "https://api.example.com/users",
        "method": "POST",
        "body": {
            "json": {
                "name": "{{ user_name }}",
                "email": "{{ email }}",
                "roles": ["user", "admin"]
            }
        }
    }
}
```

### Form Data (URL-encoded)

```json
{
    "request": {
        "url": "https://api.example.com/login",
        "method": "POST",
        "body": {
            "form": {
                "username": "{{ username }}",
                "password": "{{ password }}"
            }
        }
    }
}
```

### XML Body

```json
{
    "request": {
        "url": "https://api.example.com/soap",
        "method": "POST",
        "headers": {
            "Content-Type": "application/xml"
        },
        "body": {
            "xml": "<?xml version=\"1.0\"?><request><id>{{ id }}</id></request>"
        }
    }
}
```

### Plain Text

```json
{
    "request": {
        "url": "https://api.example.com/text",
        "method": "POST",
        "body": {
            "text": "Hello, {{ name }}!"
        }
    }
}
```

### Base64 Encoded

```json
{
    "request": {
        "url": "https://api.example.com/binary",
        "method": "POST",
        "body": {
            "base64": "SGVsbG8gV29ybGQh"
        }
    }
}
```

### Binary File

```json
{
    "request": {
        "url": "https://api.example.com/upload",
        "method": "POST",
        "body": {
            "binary": "/path/to/file.bin"
        }
    }
}
```

### File Uploads (Multipart)

```json
{
    "request": {
        "url": "https://api.example.com/upload",
        "method": "POST",
        "body": {
            "files": {
                "document": "/path/to/document.pdf",
                "image": "/path/to/image.png"
            }
        }
    }
}
```

### GraphQL

```json
{
    "request": {
        "url": "https://api.example.com/graphql",
        "method": "POST",
        "body": {
            "graphql": {
                "query": "query GetUser($id: ID!) { user(id: $id) { name email } }",
                "variables": {
                    "id": "{{ user_id }}"
                }
            }
        }
    }
}
```

## Authentication

### Scenario-Level Default

```json
{
    "auth": "mymodule:get_auth",
    "stages": [
        {
            "name": "uses_default_auth",
            "request": {"url": "https://api.example.com/protected"}
        }
    ]
}
```

### Stage-Level Override

```json
{
    "stages": [
        {
            "name": "custom_auth",
            "request": {
                "url": "https://api.example.com/special",
                "auth": {
                    "name": "mymodule:special_auth",
                    "kwargs": {
                        "role": "admin"
                    }
                }
            }
        }
    ]
}
```

### Auth Function Example

```python
# mymodule.py
import httpx

def get_auth() -> httpx.Auth:
    return httpx.BasicAuth("user", "password")

def special_auth(role: str) -> httpx.Auth:
    # Custom auth logic based on role
    return httpx.BasicAuth(role, "secret")
```

## Timeout and Redirects

```json
{
    "request": {
        "url": "https://slow-api.example.com/process",
        "timeout": 120.0,
        "allow_redirects": false
    }
}
```

Use template expressions for dynamic values:

```json
{
    "substitutions": [{"vars": {"timeout_secs": 60}}],
    "stages": [
        {
            "request": {
                "url": "https://api.example.com",
                "timeout": "{{ timeout_secs }}"
            }
        }
    ]
}
```
