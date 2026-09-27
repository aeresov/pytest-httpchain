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
        "auth": null
    }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `url` | string | required | Absolute `http`/`https` URL, or one relative to [`client.base_url`](#relative-urls), sent as written (supports templates) |
| `method` | string | `GET` | HTTP method |
| `params` | object | `{}` | Query parameters, merged into any query already in `url` (and over `client.params`) |
| `headers` | object | `{}` | Request headers (over `client.headers`) |
| `body` | object | `null` | Request body configuration |
| `auth` | string/object | `null` | Authentication (overrides scenario-level) |
| `timeout` | number | `client.timeout` (`30.0`) | Request timeout in seconds |
| `allow_redirects` | boolean | `client.follow_redirects` (`true`) | Follow redirects |

The defaults of `params`, `headers`, `timeout` and `allow_redirects` come from the scenario's
[`client` block](scenarios.md#client-configuration): what a stage sets wins. That is why the
structure above leaves `timeout` and `allow_redirects` out: written in a stage, even at the values
in the table, they override `client.timeout` and `client.follow_redirects`.

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

The URL goes to httpx exactly as written, or as its templates rendered: nothing normalizes it on
the way. An encoded dot segment such as `/static/%2e%2e/ok` or a `\` in the path reaches the server
as it appears in the scenario, and a URL may be up to 65,536 characters long, httpx's own limit.
What httpx does itself still applies: it percent-encodes characters a URL cannot carry (a space
becomes `%20`) and, like curl, resolves literal `.` and `..` segments, so a path-traversal probe
writes them encoded.

An absolute URL has an `http` or `https` scheme and a host. A literal URL is checked at
collection, a templated one once it has rendered (a template may render to a pydantic URL object,
such as a pydantic-settings field, which stands for its string), and the host and port must be well
formed. Any `{{ ... }}` in the URL makes it a templated one, so an empty `{{ }}` is refused at
collection, as in every template. The check reads the URL as a browser does. These, which a browser
accepts only by repairing them, are refused rather than repaired, since httpx would send them
unrepaired or to another host: `http:/example.com`, a leading or trailing space, a control
character anywhere (a tab or a line break, say), a `\` before the path (a browser ends the host
there, httpx does not), a percent-encoded host (a browser decodes it, httpx does not), a host only
a browser's IDNA mapping accepts, such as one in fullwidth letters or with a soft hyphen (a browser
maps it to plain ASCII, httpx does not). The host itself goes out as written, for the
resolver to read: `127.1` reaches `127.0.0.1` as in a browser, while a spelling only a browser
tidies up, such as `127.0.0.1.`, fails when the request is sent.

### Relative URLs

With a `base_url` in the scenario's [`client` block](scenarios.md#client-configuration), a URL
without a scheme is relative, and httpx appends it to the base URL's path:

```json
{
    "client": {"base_url": "https://api.example.com/v1"},
    "substitutions": [{"vars": {"user_id": 1}}],
    "stages": [
        {
            "name": "get_user",
            "request": {"url": "/users/{{ user_id }}"},
            "response": [{"verify": {"status": 200}}]
        }
    ]
}
```

This requests `https://api.example.com/v1/users/1`: the base path is kept, even though the stage's
URL starts with `/` (a browser would resolve that against the host's root), and `users/1` means the
same. The relative URL is sent as written, like an absolute one, and literal `.` and `..` segments
are resolved (`../health` reaches `https://api.example.com/health`). An absolute URL ignores
`base_url`. A relative URL must not start with `//` (httpx would drop the host after it) or `:`,
and must not start or end with a space or control character.

Without a `base_url`, a relative URL fails collection with `HTTPCHAIN034`; so does a templated one
whose text before the first template is already relative (`/users/{{ user_id }}`). A URL starting
with a template (`"{{ path }}"`) is only known once rendered, and if it renders to a relative URL
without a `base_url`, the stage fails before anything is sent. See
[Client configuration](scenarios.md#client-configuration) for the full rules.

### Query parameters

`params` is merged into a query string the URL already has, not substituted for it:

- the URL's own parameters come first, then the new keys from `params`, in their declared order;
- a key present in both takes its value from `params`, sent where the key first appears in the
  URL; its other occurrences in the URL are dropped;
- a list value repeats the key: `{"tag": ["a", "b"]}` sends `tag=a&tag=b`.

So `"url": "{{ base }}/items?page=2&sort=name"` with `"params": {"sort": "price", "limit": 10}`
sends `/items?page=2&sort=price&limit=10`.

The scenario's `client.params` only fill in: they add the keys that neither the URL's query nor
`params` sets, ahead of the new keys from `params` (where httpx puts a client's parameters). So
`client.params` `{"api_key": "k", "sort": "name"}` turns the example above into
`/items?page=2&sort=price&api_key=k&limit=10`.

The URL's query is split into parameters at `&` only, and a parameter whose name `params` does not
set goes out just as it would without `params`, in the same place: a bare `flag`, `%20` or `+`, an
escape that is not UTF-8 such as `q=%E9`, and a `;` inside a parameter are all left alone. Names
are compared decoded, so the `params` key `"sort"` also replaces `so%72t=name`. Values from
`params` are encoded as form data (a space becomes `+`, `&` becomes `%26`), and a key whose value
is an empty list is removed from the query. A value that is not a string is turned into text
first (`true`/`false` for a boolean, nothing for `null`); one that cannot be, such as a
`"{{ 2 ** 100000 }}"` past the 4300 digits Python converts to text or a fixture's object whose
`__str__` raises, fails the stage with `Cannot convert query parameter 'n' to text: ...`. The
HTTP report section and the HAR export show the URL that was sent; the report shows the values of
credential parameters such as `access_token` as `[REDACTED]` (see
[Secrets in reports](../getting-started.md#secrets-in-reports)).

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

The scenario's [`client.headers`](scenarios.md#client-configuration) are sent too, and a header
the stage sets replaces the client's of the same name, compared case-insensitively. A client
`Content-Type` labels the body of every stage that sets none, JSON included, except a body whose
encoding fixes its type: a [`form`](#form-data-url-encoded) body keeps
`application/x-www-form-urlencoded`, and a [`files`](#file-uploads-multipart) body
`multipart/form-data` with the boundary between its parts.

A failing stage's report shows the headers it sent, with the values of credential headers such as
`Authorization` as `[REDACTED]` (see [Secrets in reports](../getting-started.md#secrets-in-reports)).

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

`"json": null`, or a template that renders to `null`, sends the JSON document
`null` (with `Content-Type: application/json` unless the request sets its own).

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

Relative paths in `binary` and `files` resolve against the **scenario file's
directory** — the same rule as `$ref`/`$include` — so data files can live next
to the test that uses them, independent of where pytest is launched from.
Absolute paths pass through unchanged.

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

A stage that sets neither takes the scenario's `client.timeout` (30 seconds unless set) and
`client.follow_redirects` (`true` unless set), and a stage's own value wins even where it equals
the default. How many redirects a request follows at most is `client.max_redirects` (20): one
more fails the stage (see [Client configuration](scenarios.md#client-configuration)).

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
