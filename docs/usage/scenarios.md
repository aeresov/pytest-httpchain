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
    "substitutions": [],
    "stages": []
}
```

| Field | Type | Description |
|-------|------|-------------|
| `description` | string | Optional human-readable description |
| `marks` | array | pytest markers applied to all stages |
| `fixtures` | array | pytest fixtures available to all stages |
| `auth` | string/object | Default authentication for all requests |
| `ssl` | object | SSL/TLS configuration |
| `substitutions` | array/object | Variables and functions for the context |
| `stages` | array/object | The test stages to execute |

Field names are validated strictly at every level: an unknown or misspelled key (`"headerz"`, `"alwaysrun"`) fails validation at collection time, naming the key and its location. The only exceptions are the `$schema` editor key (discarded during validation) and the `$ref`/`$include`/`$merge` reference directives (resolved before validation).

## Stage Structure

Each stage represents a single HTTP request:

```json
{
    "name": "stage name",
    "description": "Optional description",
    "marks": [],
    "fixtures": [],
    "always_run": false,
    "substitutions": [],
    "parametrize": null,
    "parallel": null,
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
| `substitutions` | array/object | Stage-specific variables |
| `parametrize` | array/object | Parametrization steps that expand the stage into multiple test cases (see [Parametrization](../advanced/parametrization.md)) |
| `parallel` | object | Parallel execution config (`repeat`/`foreach`) for running the request concurrently (see [Parallel Execution](../advanced/parallel.md)) |
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
    connection rather than reconnecting per stage.
-   **HTTP/2 is offered** and used whenever the server negotiates it.

Each scenario gets its own client, so scenarios never share cookies or
connections with each other.

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
-   Fixtures can be referenced in *stage* templates only. Scenario-level `substitutions`, `auth`, and `ssl` resolve once per scenario (even one that runs once per param of a [parametrized fixture](#parametrized-fixtures)) — when its first stage runs (or already at collection when stage `parametrize` values contain templates, since pytest needs concrete parameter values to collect) — against a context that deliberately excludes fixture values; the validator rejects fixture references there (`HTTPCHAIN016`).
-   A fixture whose value is itself **callable** is treated as a factory: it is wrapped so `{{ my_fixture(...) }}` invokes it. The wrapper is a different object than the original callable, so attribute access on it (`{{ my_fixture.some_attr }}`) is not available — call it instead.
-   When such a call returns a **context manager**, it is entered and the template gets the value it yields. It is exited when the stage ends, whether the stage passed, failed or was skipped, and before pytest tears down the stage's fixtures, so on exit the context manager can still use the fixtures it is built on (a transaction on a `class`-scoped connection fixture, say). One entered by the request or a response step is exited as soon as the response steps are done, in the thread that ran them, and one entered by the stage's `always_run`, `substitutions` or `parallel` after that. In a `parallel` stage, each iteration exits its own when it ends, in its worker thread: a context manager tied to its thread (a `sqlite3` connection) works, and one iteration's transaction does not stay open while the others run. Several are exited in reverse order of entry, and, as with a yield fixture's teardown, an exit is not told whether the stage failed. The yielded value is only good within that stage: don't save it for a later one.
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

The scenario's `substitutions`, `auth` and `ssl` are not resolved again for each chain. They cannot refer to fixtures (see above), so no param can change them. They resolve once, when the first chain starts, and every chain uses the result. A user function there runs once, however many params there are. If resolving them fails, every later stage skips, in every chain.

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
