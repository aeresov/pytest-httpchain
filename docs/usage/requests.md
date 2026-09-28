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
| `auth` | object/string/`false` | `null` | [Authentication](#authentication) over the scenario's: `basic`, `digest`, `bearer` or a user function; `false` for none |
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
`application/x-www-form-urlencoded`, and a [`multipart` or `files`](#file-uploads-multipart)
body `multipart/form-data` with the boundary between its parts.

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

`multipart` sends a `multipart/form-data` body: form fields and files, each a
part of its own.

```json
{
    "request": {
        "url": "https://api.example.com/reports",
        "method": "POST",
        "body": {
            "multipart": {
                "fields": {
                    "title": "{{ title }}",
                    "tags": ["q3", "finance"],
                    "draft": false
                },
                "files": {
                    "document": "./report.pdf",
                    "images": [
                        "./chart.png",
                        {"path": "./photo.jpg", "filename": "cover.jpg", "content_type": "image/jpeg"}
                    ],
                    "note": {"content": "Figures are preliminary.", "filename": "note.txt", "content_type": "text/plain"}
                }
            }
        }
    }
}
```

-   **`fields`**: each value is text, a number or a boolean, sent as text the
    way a form value is (`false` as `false`, `12` as `12`); a list sends a field
    per item, all under the same name.
-   **`files`**: each field is one file or a list of them, a part per file,
    all under the same name. A file is a path, or an object with exactly one of
    `path` (a file to read), `content` (its content as text, sent UTF-8
    encoded) and `base64` (its content base64-encoded, for binary data), and
    optionally:
    -   `filename`: the part's filename. Not set, it is the path's last
        component, or for `content` and `base64` the field's name. `""` sends
        the part without one, which most servers read as a form field with a
        content type of its own, such as a JSON part beside a file.
    -   `content_type`: the part's `Content-Type`. Not set, it is guessed from
        the filename's extension, else `application/octet-stream`.

At least one of `fields` and `files` is set. The fields are sent first, then
the files, each in the order written. A list with nothing in it (a template can
render one) sends no part, and a body left with no part at all is still sent,
as an empty multipart body. A template can stand for any value: a field, a
list of them, a path, a key of a file object, a whole file object or a list of
files (`"images": "{{ uploads }}"`).

`files` is the same body without form fields, and takes the same forms of file:

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

The request's `Content-Type` is `multipart/form-data` with the boundary between
the parts, set over a [client](scenarios.md#client-configuration) `Content-Type`
too. A stage's own `Content-Type` is sent as written, and the parts are
delimited by the boundary it names (`multipart/related; boundary=abc`). A
multipart type that names none, as a `"Content-Type": "multipart/form-data"`
written out of habit, is sent with the body's boundary added
(`multipart/form-data; boundary=...`): without one, the server could not find
the parts. One that names an empty boundary fails the stage, and so does one
naming a boundary the parts cannot be delimited by as written: holding a `;`,
ending in whitespace, or starting or ending with a quote
(`boundary="a;b"`), none of which RFC 2046 allows in a boundary.

A file that is not there fails the stage before anything is sent, naming its
path as the scenario gives it rather than where it resolved to, in the tidied
form any path takes (`./report.pdf` fails with
`File not found for upload: report.pdf`, as a missing `binary` file does);
`validate --deep` reports it ahead of the run
([`HTTPCHAIN020`](../diagnostics.md)). A
`filename` or `content_type` template that renders to `null` fails the stage
too, rather than sending the default one (see
[Templates that render to `null`](substitutions.md#templates-that-render-to-null)).

A failing stage's report shows the body part by part, a binary part as its
size, cut at 1000 characters like any body; the [HAR export](../getting-started.md#har-export)
records the body as sent (base64-encoded when a part is binary).

Relative paths in `binary`, and the files of `files` and `multipart` (a path,
or a file object's `path`), resolve against the **scenario file's directory** —
the same rule as `$ref`/`$include` — so data files can live next to the test
that uses them, independent of where pytest is launched from. Absolute paths
pass through unchanged.

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

A scenario's `auth` applies to every request, and a request's `auth` replaces it for that request.
Three schemes are built in:

| `auth` | Sends |
|--------|-------|
| `{"basic": {"username": "...", "password": "..."}}` | HTTP Basic credentials with every request |
| `{"digest": {"username": "...", "password": "..."}}` | HTTP Digest: the credentials answer the server's challenge |
| `{"bearer": "..."}` | `Authorization: Bearer <token>` |

```json
{
    "substitutions": [{"vars": {"api_user": "{{ env('API_USER') }}", "api_password": "{{ env('API_PASSWORD') }}"}}],
    "auth": {"basic": {"username": "{{ api_user }}", "password": "{{ api_password }}"}},
    "stages": [
        {
            "name": "uses_the_scenario_auth",
            "request": {"url": "https://api.example.com/protected"},
            "response": [{"verify": {"status": 200}}]
        }
    ]
}
```

The values may be templates. A scenario's `auth` resolves once, when its first stage runs, against
the scenario substitutions only, like [`ssl` and `client`](scenarios.md#fixtures): the validator
reports a fixture (`HTTPCHAIN016`) or an undefined name (`HTTPCHAIN017`) in it. A request's
resolves with the rest of the request, for each stage and each iteration of a
[parallel](../advanced/parallel.md) stage, so it can use a token an earlier stage saved:

```json
{
    "stages": [
        {
            "name": "login",
            "request": {
                "url": "https://api.example.com/login",
                "method": "POST",
                "body": {"json": {"username": "demo", "password": "{{ env('DEMO_PASSWORD') }}"}}
            },
            "response": [{"verify": {"status": 200}}, {"save": {"jmespath": {"token": "access_token"}}}]
        },
        {
            "name": "profile",
            "request": {"url": "https://api.example.com/me", "auth": {"bearer": "{{ token }}"}},
            "response": [{"verify": {"status": 200}}]
        }
    ]
}
```

-   Basic and bearer set the `Authorization` header, replacing one the stage's `headers` or
    [`client.headers`](scenarios.md#client-configuration) carry. Digest replaces it when it
    answers the server's challenge: a request with no challenge to answer yet goes out with the
    header the stage carries, and if the server accepts that, digest is not used.
-   A credential whose template renders to `null` fails the stage (for the scenario's `auth`, its
    initialization), naming the field: `'request.auth.bearer' was declared as '{{ token }}' but
    rendered to None`. The request is not sent unauthenticated, nor with the scenario's
    credentials. A bearer token must not be empty either, and every value must be a string.
-   A credential that fails validation is not quoted in the failure, and the report shows the
    `Authorization` header as `[REDACTED]` (see
    [Secrets in reports](../getting-started.md#secrets-in-reports)).
-   Digest sends a request without credentials first, and sends it again with the answer to the
    server's `401` challenge. The scenario's digest auth is one for all its requests, so after the
    first challenge it answers up front with the server's nonce, a parallel stage's concurrent
    requests included, counting each use of the nonce once (`nc`), as a server that checks for
    replayed answers requires; a request's own is new for each request, which is challenged every
    time. A failing stage's report labels its request `(after 1 auth exchange)`, and the HAR
    export has both requests.
-   The whole `auth` may be one template, such as `"{{ creds }}"` over a `vars` object written as
    `{"basic": {...}}`: it is the scheme it renders to. At scenario level, it resolves once, as
    above. A string it renders is a user function's name, so a token goes in
    `{"bearer": "{{ token }}"}`: `"auth": "{{ token }}"` fails. So do credentials rendered as one
    `"user:password"` string, which has the shape of a name and fails to import as one. Neither
    failure quotes the string: a user function name that a template in `auth` rendered is never
    shown, only why it failed (`its module has no function of that name`).

### Turning Auth Off for One Request

`"auth": false` sends a request as a scenario without `auth` would, for a public endpoint in an
authenticated scenario:

```json
{
    "auth": {"bearer": "{{ env('API_TOKEN') }}"},
    "stages": [
        {
            "name": "health_check_is_public",
            "request": {"url": "https://api.example.com/health", "auth": false},
            "response": [{"verify": {"status": 200}}]
        }
    ]
}
```

It turns off the scheme only: an `Authorization` header the stage or `client.headers` set is
still sent, and so is a URL's `user:password@`, which httpx sends as Basic credentials. `false`
belongs in a request; at scenario level, leave `auth` out.

### Custom Schemes: User Functions

Any other scheme (OAuth2 client credentials, request signing, a token refreshed on expiry) is a
Python function that returns an `httpx.Auth` (or anything else httpx takes as `auth`), named as
`"module:function"`, or as an object with `name` and the `kwargs` to call it with:

```json
{
    "auth": "mymodule:get_auth",
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
            },
            "response": [{"verify": {"status": 200}}]
        }
    ]
}
```

```python
# mymodule.py
import httpx


def get_auth() -> httpx.Auth:
    return httpx.BasicAuth("user", "password")


def special_auth(role: str) -> httpx.Auth:
    # Custom auth logic based on role
    return httpx.BasicAuth(role, "secret")
```

The `kwargs` values may be templates, resolved as the built-ins' are, and so may the name, which a
failure to import then does not show (it may be a credential, see above). An object is a user
function when it has `name`, and a built-in when it has `basic`, `digest` or `bearer`; one with
several of these keys fails validation, naming the extra one (`auth -> basic -> name: Extra inputs
are not permitted`). See [httpx's guide](https://www.python-httpx.org/advanced/authentication/#custom-authentication-schemes)
for writing an `httpx.Auth`.

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
