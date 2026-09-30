# Scenarios and Stages

## Scenario Structure

A scenario is a JSON file that defines a complete test case. The basic structure:

```json
{
    "description": "Optional description of this scenario",
    "marks": [],
    "fixtures": [],
    "auth": null,
    "ssl": {},
    "client": {},
    "substitutions": [],
    "stages": []
}
```

| Field | Type | Description |
|-------|------|-------------|
| `description` | string | Optional human-readable description |
| `marks` | array | pytest markers applied to all stages |
| `fixtures` | array | pytest fixtures available to all stages |
| `auth` | object/string | Authentication for every request: `basic`, `digest`, `bearer` or a user function (see [Authentication](requests.md#authentication)) |
| `ssl` | object | SSL/TLS configuration |
| `client` | object | The shared HTTP client: base URL, default headers and query parameters, timeout, redirects, proxy, pool (see [Client configuration](#client-configuration)) |
| `substitutions` | array/object | Variables and functions for the context |
| `stages` | array/object | The test stages to execute |

Field names are validated strictly at every level: an unknown or misspelled key (`"headerz"`, `"alwaysrun"`) fails validation at collection time, naming the key and its location. The only exceptions are the `$schema` editor key (discarded during validation) and the `$ref`/`$include`/`$merge` reference directives (resolved before validation).

Scenario files, and every file they pull in (`$include`/`$merge`/`$ref` targets, `verify.body.schema` files), are read as UTF-8. A leading byte-order mark, which some editors on Windows write, is accepted. A scenario, or a file it `$include`s, `$merge`s or `$ref`s, in another encoding such as Latin-1 fails to load with `HTTPCHAIN014`, naming the file that is not UTF-8. A `verify.body.schema` file in another encoding is read only when its stage runs, and fails that stage; `validate --deep` reports it ahead of time as `HTTPCHAIN021`.

## Comments and trailing commas

Every JSON file the plugin reads from disk is JSON with comments (JSONC): `//` line comments, `/* ... */` block comments, and a trailing comma before a closing `]` or `}`. That holds for scenario files, the files they `$include`, `$merge` or `$ref`, `verify.body.schema` files and the files their `$ref`s reach, at collection, at runtime and in every [command](../cli.md). Name a scenario `test_<name>.http.jsonc` and it is collected exactly as `test_<name>.http.json` is; the extension only tells your editor to expect comments. A `.json` file may carry them too:

```json
// Log in, then read the profile with the token the login returned.
{
    "stages": [
        {
            "name": "login",
            "request": {
                "url": "https://api.example.com/login",
                "method": "POST",
                "body": { "json": { "user": "alice", "password": "secret" } }, // test credentials
            },
            "response": [
                { "verify": { "status": 200 } },
                { "save": { "jmespath": { "token": "access_token" } } },
            ],
        },
        {
            "name": "profile",
            /* The token saved above authorizes this request. */
            "request": {
                "url": "https://api.example.com/me",
                "auth": { "bearer": "{{ token }}" },
            },
            "response": [{ "verify": { "status": 200 } }],
        },
    ],
}
```

- **Strictly valid JSON reads as before.** There is no switch: comments and trailing commas are simply also accepted, in `.json` and `.jsonc` files alike.
- **One trailing comma.** A comma directly before `]` or `}` (whitespace and comments may sit between them) is dropped. A leading comma (`[,1]`), a doubled one (`[1,,]`) or one after a key's colon is still an error.
- **Nothing inside a string is touched.** `"https://example.com/a//b"` and `"/* not a comment */"` are values.
- **No other extensions.** `NaN`, `Infinity` and `-Infinity`, which Python's JSON parser reads as numbers, are syntax errors at their position, as in any strict JSON parser: JSON has no such numbers. So is a number too large for a float, such as `1e400`, which Python reads as infinity. To expect one, write it as a template: `"rate": "{{ float('inf') }}"`.
- **Block comments do not nest.** A block comment ends at its first `*/`. One that is never closed is a syntax error at the line and column of its `/*`, and fails like any other (next point).
- **Error positions are the file's.** Comments are read as whitespace, so the line and column of any JSON syntax error point into the file as you wrote it. In a scenario, or a file it `$include`s, `$merge`s or `$ref`s, the error fails the load with `HTTPCHAIN014`, which names the file when it is not the scenario itself. A `verify.body.schema` file is read only when its stage runs, so an error there fails that stage; `validate --deep` reports it ahead of time as `HTTPCHAIN021`.
- **Only files.** A response body, and anything else received over HTTP, is parsed as plain JSON, without comments or trailing commas, by Python's JSON parser, as `httpx`'s `response.json()` parses it: `NaN`, `Infinity` and `-Infinity` a server sends are read as the numbers they stand for, so a stage can still check a body a Python server wrote with them. A file a request sends (a `binary` body, a `files` or `multipart` upload) is sent byte for byte, comments and all.
- **`resolve` prints strict JSON.** It shows the scenario collection sees, with the comments and trailing commas gone (see [`resolve`](../cli.md#resolve)).

A `test_login.http.json` and a `test_login.http.jsonc` in one directory are two scenarios: both are collected, and the extension in each node id (`test_login.http.json::login::...`, `test_login.http.jsonc::login::...`) keeps their tests, HAR files and xdist groups apart; a warning about one of them names it by its node id (`test_login.http.jsonc::login`). Rename rather than copy when converting a file.

Editors know `.jsonc` as JSON with comments (VS Code opens it in its *JSON with Comments* mode, which accepts comments and warns on trailing commas). To keep schema autocompletion for those files, add their pattern to the schema mapping (see [IDE Support](../getting-started.md#ide-support)).

## Stage Structure

Each stage represents a single HTTP request:

```json
{
    "name": "stage name",
    "description": "Optional description",
    "marks": [],
    "fixtures": [],
    "always_run": false,
    "skip_if": false,
    "substitutions": [],
    "parametrize": null,
    "parallel": null,
    "retry": null,
    "request": {},
    "response": []
}
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Stage identifier (used in test names, so it cannot contain `::`, pytest's node-id separator) |
| `description` | string | Optional description |
| `marks` | array | pytest markers for this stage |
| `fixtures` | array | pytest fixtures for this stage |
| `always_run` | boolean or template | Execute even if prior stages failed |
| `skip_if` | boolean or template | Skip the stage when true, deciding when it is about to run (see [Skipping a stage at runtime](#skipping-a-stage-at-runtime)) |
| `substitutions` | array/object | Stage-specific variables |
| `parametrize` | array/object | Parametrization steps that expand the stage into multiple test cases (see [Parametrization](../advanced/parametrization.md)) |
| `parallel` | object | Parallel execution config (`repeat`/`foreach`) for running the request concurrently (see [Parallel Execution](../advanced/parallel.md)) |
| `retry` | object | Attempt the stage again while it fails, after a wait: polling until the response steps pass (see [Retries and Polling](../advanced/retry.md)) |
| `request` | object | HTTP request configuration |
| `response` | array/object | Response processing steps |

## Stages as List vs Dictionary

Stages can be defined as a list or dictionary. With dictionary format, keys become stage names; a `name` inside the stage is overridden by its key:

**List format:**

```json
{
    "stages": [
        {
            "name": "login",
            "request": {"url": "https://api.example.com/login"}
        },
        {
            "name": "get_data",
            "request": {"url": "https://api.example.com/data"}
        }
    ]
}
```

**Dictionary format:**

```json
{
    "stages": {
        "login": {
            "request": {"url": "https://api.example.com/login"}
        },
        "get_data": {
            "request": {"url": "https://api.example.com/data"}
        }
    }
}
```

## Multi-Stage Execution

Stages execute in order. If one fails, subsequent stages are skipped unless `always_run` is set:

```json
{
    "stages": [
        {
            "name": "setup",
            "request": {"url": "https://api.example.com/setup", "method": "POST"}
        },
        {
            "name": "test",
            "request": {"url": "https://api.example.com/test"}
        },
        {
            "name": "cleanup",
            "always_run": true,
            "request": {"url": "https://api.example.com/cleanup", "method": "DELETE"}
        }
    ]
}
```

The `cleanup` stage runs even if `setup` or `test` fails.

`always_run` also accepts a template expression, evaluated (with Python truthiness) at the moment a stage is about to be skipped after a failure. In scope are fixtures, parametrize parameters, scenario-level substitutions, and variables saved by earlier stages — but *not* the stage's own substitutions, which are only processed once the stage actually runs:

```json
{
    "stages": [
        {
            "name": "create",
            "request": {"url": "https://api.example.com/resource", "method": "POST"},
            "response": [{"save": {"jmespath": {"resource_id": "id"}}}]
        },
        {
            "name": "test",
            "request": {"url": "https://api.example.com/test"}
        },
        {
            "name": "cleanup",
            "always_run": "{{ exists('resource_id') }}",
            "request": {"url": "https://api.example.com/resource/{{ resource_id }}", "method": "DELETE"}
        }
    ]
}
```

After a failure, `cleanup` runs only if `create` got far enough to save `resource_id` — there is nothing to delete otherwise. A stage that fails discards its saves, which is why the example guards with `exists()` instead of referencing `resource_id` directly. The result is plain Python truthiness, so beware that a saved *string* `"false"` is truthy. The validator checks `always_run` references against this scope (`HTTPCHAIN003`/`HTTPCHAIN004`).

### Skipping a stage at runtime

A `skip` mark decides at collection, and `always_run` only counts once a stage has failed. `skip_if` skips a stage on a condition known only when the stage is about to run: a setting, a fixture, or what an earlier stage saved.

```json
{
    "substitutions": [{"vars": {"target": "{{ env('TARGET_ENV', 'dev') }}"}}],
    "stages": [
        {
            "name": "login",
            "request": {"url": "https://api.example.com/login", "method": "POST"},
            "response": [{"save": {"jmespath": {"token": "token", "mfa_required": "mfa"}}}]
        },
        {
            "name": "confirm_mfa",
            "skip_if": "{{ not mfa_required }}",
            "request": {"url": "https://api.example.com/mfa", "method": "POST", "headers": {"Authorization": "Bearer {{ token }}"}}
        },
        {
            "name": "reset_data",
            "skip_if": "{{ target == 'prod' }}",
            "request": {"url": "https://api.example.com/reset", "method": "POST", "headers": {"Authorization": "Bearer {{ token }}"}}
        }
    ]
}
```

`confirm_mfa` runs only when the login asked for it, and `reset_data` never against production.

-   **When**: `skip_if` is evaluated when the stage is about to run, after the stage's own `substitutions` and before its `parallel` config and any request. In a chain a failure aborted, a stage without `always_run` skips as ever ("Flow aborted") and its `skip_if` is not evaluated; one that `always_run` lets through still skips when its `skip_if` holds.
-   **Scope**: what the stage's request sees but the iteration parameters: fixtures, parametrize parameters, scenario-level substitutions, variables saved by earlier stages, and the stage's own substitutions. Not the `parallel.foreach` parameters, since the stage decides once for all its iterations, and not `response`, since nothing has been sent yet. A parametrized stage decides for each of its parameters.
-   **The result must be a boolean**, as a verify expression's must: a template that evaluates to anything else fails the stage, `skip_if must evaluate to bool, got str from '{{ flag }}'`, rather than skip it, or run it, by truthiness, which would silently read a saved string `"false"` as true or a `null` from a missing key as false. The message names the value's type, not the value, which may be a credential (`{{ token }}`). Compare explicitly (`{{ flag == 'yes' }}`) or convert (`{{ bool(count) }}`). `true` and `false` can be written as they are: `"skip_if": true` skips the stage without even running its substitutions.
-   **A skip is no failure**: the stage is reported skipped with the reason `skip_if: <the template>`, sends nothing, saves nothing, and the stages after it run. A context manager a factory fixture returned for its `substitutions` or `skip_if` is exited as the stage ends, as for any stage.

Because the chain goes on, a later stage finds nothing that a skipped stage would have saved, or, when an earlier stage saved the same name, that stage's value: a `refresh` stage with a `skip_if` that re-saves the `token` a `login` saved leaves the login's token in place when it skips. Read a name that may be missing with `get()` and a default (`{{ get('token', 'anonymous') }}`). The validator reports a name that only stages with a `skip_if` save, read directly by a later stage, as potentially undefined (`HTTPCHAIN003`), in whichever of the later stage's fields reads it. A stage that never skips (no `skip_if`, or `skip_if: false`) saving the same name too, or a fixture or substitution of that name where it is read, settles it. The validator does not evaluate conditions, so a stage that itself skips unless the name is there (`"skip_if": "{{ not exists('token') }}"`) is reported all the same: read the name with `get()` there too. The saves of a stage that may *fail* count as there: a failure aborts the chain, and a stage after it runs only with `always_run`, which is why the `always_run` example above guards with `exists()`. `skip_if` references themselves are checked against the scope above (`HTTPCHAIN003`/`HTTPCHAIN004`).

### The shared HTTP client

Every stage in a scenario uses one `httpx.Client`, built lazily on the first
stage that runs and closed when the scenario finishes. Three consequences worth
knowing:

-   **Cookies persist across stages.** A `Set-Cookie` from one stage is sent by
    every later stage automatically — no `save` step needed. This also holds
    across the runs of a stage's `parametrize`, so a stage that depends on
    starting cookie-free should not rely on stage parametrization to isolate
    it. A scenario that runs once per param of a
    [parametrized fixture](#parametrized-fixtures) gets a fresh client for each.
-   **Connections are pooled**, so a chain against one host reuses the same
    connection rather than reconnecting per stage. Over HTTP/1.1 the pool opens
    as many connections as there are requests in flight: a `parallel` stage's
    `max_concurrency` is what bounds them, unless `client.max_connections` sets
    a limit.
-   **HTTP/2 is offered** and used whenever the server negotiates it, unless
    `client.http2` is `false`. The requests to that server then share one
    connection, at most 100 of them at a time (see
    [Client configuration](#client-configuration)).

Each scenario gets its own client, so scenarios never share cookies or
connections with each other.

### Client configuration

The scenario's `client` block configures that client. Every field is optional,
and the defaults are what a scenario without the block gets:

```json
{
    "substitutions": [
        {"vars": {"api_root": "{{ env('API_ROOT', 'https://api.example.com/v1') }}", "api_key": "{{ env('API_KEY', 'dev-key') }}"}}
    ],
    "client": {
        "base_url": "{{ api_root }}",
        "headers": {"Accept": "application/json", "X-Api-Key": "{{ api_key }}"},
        "params": {"locale": "en"},
        "timeout": 10,
        "follow_redirects": true,
        "max_redirects": 5,
        "http2": true,
        "max_connections": null,
        "max_keepalive_connections": 20
    },
    "stages": [
        {
            "name": "get_user",
            "request": {"url": "/users/1"},
            "response": [{"verify": {"status": 200}}]
        },
        {
            "name": "slow_report",
            "request": {"url": "/reports/42", "timeout": 120},
            "response": [{"verify": {"status": 200}}]
        }
    ]
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `base_url` | string | none | Absolute `http`/`https` URL, without a query or fragment, that a relative `request.url` is appended to |
| `headers` | object | `{}` | Headers sent with every request |
| `params` | object | `{}` | Query parameters sent with every request |
| `timeout` | number | `30.0` | Timeout in seconds for the stages that do not set `request.timeout` |
| `follow_redirects` | boolean | `true` | Whether the stages that do not set `request.allow_redirects` follow redirects |
| `max_redirects` | integer | `20` | Redirects a request follows at most; one more fails the stage |
| `proxy` | string | none | Proxy URL (`http://`, `https://`, `socks5://` or `socks5h://`) for every request |
| `http2` | boolean | `true` | Offer HTTP/2, used when the server negotiates it; the requests to that server then share one connection, 100 at a time at most |
| `max_connections` | integer or `null` | `null` | Connections the pool opens at most; `null` for no limit |
| `max_keepalive_connections` | integer or `null` | `20` | Idle connections kept open for reuse at most; `null` for no limit |

**A relative URL** needs `base_url`, and httpx appends it to the base URL's path,
putting a `/` between the two: a leading `/` on the stage's URL does *not* return
to the host's root, as it would in a browser link. So with
`"base_url": "https://api.example.com/v1"` (or `.../v1/`, the same thing):

| `request.url` | Sent to |
|---------------|---------|
| `/users/1` or `users/1` | `https://api.example.com/v1/users/1` |
| `/users?page=2` | `https://api.example.com/v1/users?page=2` |
| `?page=2` | `https://api.example.com/v1/?page=2` |
| `../health` | `https://api.example.com/health` (httpx resolves literal `.` and `..` segments, as in an absolute URL) |
| `https://other.example.com/x` | `https://other.example.com/x` (an absolute URL ignores `base_url`) |

Anything that does not start with a scheme such as `https:` is a relative URL, and it is sent
as written, like an absolute one (see [URL and Query Parameters](requests.md#url-and-query-parameters)).
A relative URL without `base_url` fails collection with `HTTPCHAIN034`, as does
one whose text before its first template is relative (`/users/{{ id }}`). One
starting with a template (`{{ path }}`) is only known once rendered: if it
renders relative without a `base_url`, its stage fails, before anything is sent,
with `Request URL '/users/1' is relative, but the scenario sets no client.base_url to resolve it against`.
`base_url` has no query or fragment, since httpx would append the stage's path
after them: default query parameters belong in `params`.

**What a stage says wins** over the client's defaults:

-   `request.headers` override `headers` by name, case-insensitively: a stage's
    `accept` replaces the client's `Accept`, and the stage's spelling is sent.
-   A `Content-Type` in `headers` labels every request body, JSON included,
    except those whose encoding fixes their type: a `form` body keeps
    `application/x-www-form-urlencoded`, and a `multipart` or `files` body
    `multipart/form-data` with the boundary between its parts, which the
    client's could not name.
    A stage's own `Content-Type` wins over any of them, and a multipart one
    that names no boundary gets the body's (see
    [File Uploads](requests.md#file-uploads-multipart)).
-   `params` fill in only the keys the request does not set, neither in its
    URL's query nor in `request.params`: `"url": "/search?locale=de"` keeps
    `locale=de`, and a stage's `"params": {"locale": []}` sends no `locale` at
    all. The client's keys come before the stage's new ones, and the URL's
    query is kept as written, as with `request.params`.
-   `timeout` and `follow_redirects` apply to the stages that do not set
    `request.timeout` or `request.allow_redirects`. A stage that sets one wins
    even when its value equals the default (`"timeout": 30.0` against a client
    `timeout` of 10), and so does one it takes from an `$include` or `$merge`.

**Templates** in `client` resolve once per scenario, when its first stage runs,
against the scenario substitutions only, like [`ssl` and `auth`](#fixtures):
fixtures are not available there (`HTTPCHAIN016`), nor are saved values
(`HTTPCHAIN017`). A value from the environment comes through `env()`, as in the
example. A template on `base_url`, `proxy`, `max_connections` or
`max_keepalive_connections` that renders to `null` fails the scenario's
initialization instead of dropping the setting (see
[Templates that render to `null`](substitutions.md#templates-that-render-to-null));
a literal `null` on a pool limit means "no limit".

**`proxy`** carries every request of the scenario and replaces the proxy
settings httpx reads from the environment (`HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY` and `NO_PROXY` alike). Credentials in its URL
(`http://user:pass@proxy:3128`) authenticate with the proxy. The `socks5` and
`socks5h` schemes need httpx's SOCKS support, `pip install 'httpx[socks]'`.
An `https://` proxy's own TLS connection follows [`ssl`](#ssl-configuration)
as the servers' do once `ssl` sets `verify: false`, a CA bundle or a `cert`:
the proxy's certificate is checked the same way, and the client certificate is
offered to a proxy that asks for one. With the default `ssl`, the proxy's
certificate is checked as httpx checks one from `HTTPS_PROXY`: against the
system's CA store and certifi's bundle.
A `client` value that fails validation, such as a proxy URL from the
environment with an unencoded `/` in its password, is reported without the
value, which may be a credential: the message says what is wrong with it, and
the scenario's initialization failure is every later stage's skip reason too.

**The connection pool** has no limit by default: over HTTP/1.1,
`max_concurrency` bounds a `parallel` stage's connections. httpx's own default
is 100 connections, which the plugin used to keep, so a stage with a higher
`max_concurrency` had the rest of its requests wait for a connection. Set
`max_connections` to cap the connections a server sees from the scenario.

Over HTTP/2, which an HTTPS server may negotiate, the pool does not come into
it: the requests to that server share a single connection, which httpx holds
to 100 requests at once (fewer if the server allows fewer), the rest waiting
their turn. A `parallel` stage that needs more in flight against such a server
sets `http2` to `false`, for an HTTP/1.1 connection per request.

`ssl` stays a block of its own (see [SSL Configuration](#ssl-configuration)).

## Pytest Integration

### Markers

Apply pytest markers at scenario or stage level:

```json
{
    "marks": ["slow", "integration"],
    "stages": [
        {
            "name": "skipped_stage",
            "marks": ["skip(reason='not implemented')"],
            "request": {"url": "https://api.example.com"}
        }
    ]
}
```

Supported marker formats:

-   `"skip"` - simple marker
-   `"skip(reason='message')"` - marker with arguments
-   `"xfail"` - expected failure
-   `"usefixtures('fixture_name')"` - trigger a fixture's setup/teardown for the stage

!!! note
    `usefixtures` only runs the fixture (for its side effects/setup); it does **not**
    make the fixture's value available to `{{ }}` templates. To use a fixture value
    in templates, list it in the stage or scenario [`fixtures`](#fixtures) array.

An `xdist_group` marker goes in the scenario's `marks`, never a stage's: see
[pytest-xdist](../advanced/parallel.md#running-scenarios-in-parallel-with-pytest-xdist).

### Fixtures

Fixtures are loaded into the common data context:

```python
# conftest.py
import pytest


@pytest.fixture
def api_token():
    return "secret-token-123"


@pytest.fixture
def base_url():
    return "https://api.example.com"
```

```json
{
    "fixtures": ["base_url"],
    "stages": [
        {
            "name": "authenticated_request",
            "fixtures": ["api_token"],
            "request": {
                "url": "{{ base_url }}/protected",
                "headers": {
                    "Authorization": "Bearer {{ api_token }}"
                }
            }
        }
    ]
}
```

Scenario-level fixtures are requested by every stage. A few consequences to keep in mind:

-   Fixture values take precedence over previously saved variables of the same name, so don't save under a scenario fixture's name (the validator warns with `HTTPCHAIN009`).
-   pytest scoping still applies: a function-scoped fixture is set up again for each stage. Use `class`- or `session`-scoped fixtures for state that must survive across stages.
-   Fixtures can be referenced in *stage* templates only. Scenario-level `substitutions`, `auth`, `ssl` and `client` resolve once per scenario (even one that runs once per param of a [parametrized fixture](#parametrized-fixtures)) — when its first stage runs (or already at collection when stage `parametrize` values contain templates, since pytest needs concrete parameter values to collect) — against a context that deliberately excludes fixture values; the validator rejects fixture references there (`HTTPCHAIN016`).
-   A fixture whose value is itself **callable** is treated as a factory: it is wrapped so `{{ my_fixture(...) }}` invokes it. The wrapper is a different object than the original callable, so attribute access on it (`{{ my_fixture.some_attr }}`) is not available — call it instead.
-   When such a call returns a **context manager**, it is entered and the template gets the value it yields. It is exited when the stage ends, whether the stage passed, failed or was skipped, and before pytest tears down the stage's fixtures, so on exit the context manager can still use the fixtures it is built on (a transaction on a `class`-scoped connection fixture, say). One entered by the request or a response step is exited as soon as the response steps are done (with a [`retry`](../advanced/retry.md#factory-fixtures), the last attempt's: each attempt enters its own), in the thread that ran them, and one entered by the stage's `always_run`, `substitutions`, `skip_if`, `parallel` or `retry` after that. In a `parallel` stage, each iteration exits its own when it ends, in its worker thread: a context manager tied to its thread (a `sqlite3` connection) works, and one iteration's transaction does not stay open while the others run. Several are exited in reverse order of entry, and, as with a yield fixture's teardown, an exit is not told whether the stage failed. The yielded value is only good within that stage: don't save it for a later one.
-   An exception raised when exiting such a context manager fails the stage, even a skipped one or one a user function xfailed, with a message naming the fixture (`Exiting the context manager from fixture 'transaction' failed: ...`). If the stage had already failed, its own failure message comes first. In a `parallel` stage it fails the iteration, which cancels the others, as any failing iteration does; a failing iteration cancels them before its own exits, so no queued iteration sends its request while a slow exit (a rollback, say) runs. An iteration still running when another one fails or skips the stage exits its own when it ends all the same. In a `parallel` stage, what an iteration's exits raise is labelled with the iteration (`Iteration 1: Exiting the context manager from fixture 'transaction' failed: ...`), bar the first line of the stage's failure, which names the iteration already (`Parallel execution failed at iteration 1: ...`); the lines left unlabelled come from the context managers the stage entered outside its iterations (in `substitutions`, say). Like any failure, it discards the stage's saves and aborts the chain.

#### Parametrized fixtures

A `class`-scoped (or `module`, `package`, `session`) fixture with `params` that every stage requests, such as a scenario-level fixture, runs the whole scenario once per param. The same goes for such a fixture that a requested fixture depends on. Each param's stages run as a chain of their own, in stage order, one chain after the other in the order pytest puts the params. The fixture is set up once per param:

```python
# conftest.py
@pytest.fixture(scope="class", params=["acme", "globex"])
def tenant(request):
    return request.param
```

```json
{
    "fixtures": ["tenant"],
    "stages": [
        {
            "name": "create",
            "request": {"url": "https://api.example.com/{{ tenant }}/items", "method": "POST"},
            "response": [{"save": {"jmespath": {"item_id": "id"}}}]
        },
        {
            "name": "read",
            "request": {"url": "https://api.example.com/{{ tenant }}/items/{{ item_id }}"}
        }
    ]
}
```

This runs `create[acme]`, `read[acme]`, `create[globex]`, `read[globex]`. Each chain starts the way the scenario's first run does. Nothing the previous chain saved is visible, and a stage failure there does not skip this one. The chain gets its own [HTTP client](#the-shared-http-client), so no cookies carry over.

The scenario's `substitutions`, `auth`, `ssl` and `client` are not resolved again for each chain. They cannot refer to fixtures (see above), so no param can change them. They resolve once, when the first chain starts, and every chain uses the result. A user function there runs once, however many params there are. If resolving them fails, every later stage skips, in every chain.

If you run only some of the tests, with `-k`, `--lf` or `--deselect`, and keep a later stage of a chain without an earlier one, pytest-httpchain warns and names the chain, as in `the chain for tenant='acme'`. The later stage runs without what the earlier one would have saved.

A fixture with `params` does not split the scenario when it is function-scoped, or when only some stages request it (the stages without it belong to no single param). It varies in place instead, like a stage's [`parametrize`](../advanced/parametrization.md): each stage that requests it runs once per param, and all of them share one chain. This depends on which stages request the fixture, not on which of them you run: selecting only those stages, with `-k` or by node id (as an IDE runs a single test), does not split the scenario either.

Varying in place is rarely what you want from a `class`-scoped (or broader) fixture that two or more stages request. The stages still run in stage order, so each of them runs for every param before the next one does. With `tenant` listed in the `fixtures` of `create` and `read` only, that is `create[acme]`, `create[globex]`, `read[acme]`, `read[globex]`: `read[acme]` sees what `create[globex]` saved, and the fixture is set up again each time its param changes, four times instead of twice. pytest-httpchain warns about this at collection (`the class-scoped fixture 'tenant' has params and is requested by stages ['create', 'read'] but not by every stage`). To run the whole chain once per param instead, request the fixture from every stage, most simply in the scenario's `fixtures`.

A scenario's chains always run together, so a `package`- or `session`-scoped fixture with `params` that several scenarios request is set up once per param in each scenario.

## SSL Configuration

Configure SSL/TLS at the scenario level:

```json
{
    "ssl": {
        "verify": true,
        "cert": null
    },
    "stages": [...]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `verify` | bool/string | `true` (verify), `false` (skip), or path to CA bundle |
| `cert` | string/array | Client certificate path, or `[cert_path, key_path]` |

Once set, they apply to the TLS connection to an `https://`
[`client.proxy`](#client-configuration) as well.

**Disable verification (development only):**

```json
{
    "ssl": {"verify": false}
}
```

**Custom CA bundle:**

```json
{
    "ssl": {"verify": "/path/to/ca-bundle.crt"}
}
```

**Client certificate:**

```json
{
    "ssl": {
        "cert": ["/path/to/client.crt", "/path/to/client.key"]
    }
}
```
