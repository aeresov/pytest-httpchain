# Getting Started

## Installation

Install via pip from PyPI:

```bash
pip install pytest-httpchain
```

Or directly from GitHub:

```bash
pip install 'git+https://github.com/aeresov/pytest-httpchain@main'
```

## Configuration

Configuration options can be set in `pytest.ini` or `pyproject.toml` under `[tool.pytest.ini_options]`.

| Option | Default | Description |
|--------|---------|-------------|
| `httpchain_suffix` | `http` | File suffix for test discovery. Files must match `test_<name>.<suffix>.json` |
| `httpchain_ref_parent_traversal_depth` | `3` | Maximum parent directory traversals allowed in `$include`/`$merge`/`$ref` paths, and in a `verify.body.schema`'s JSON Schema `$ref`s to files |
| `httpchain_max_comprehension_length` | `50000` | Maximum length for list/dict comprehensions in template expressions |
| `httpchain_max_parallel_iterations` | `10000` | Maximum number of parallel iterations (`repeat`/`foreach`) allowed per stage |
| `httpchain_redact_headers` | `Authorization Proxy-Authorization Cookie Set-Cookie X-API-Key API-Key X-Auth-Token` | Headers whose values reports and failure messages show as `[REDACTED]`. Case-insensitive; empty disables. See [Secrets in reports](#secrets-in-reports) |
| `httpchain_redact_query_params` | `access_token refresh_token id_token api_key apikey client_secret password token` | Query parameters whose values URLs in reports show as `[REDACTED]`. Case-insensitive; empty disables |
| `httpchain_har_redact` | `false` | Apply the same redaction to [HAR files](#har-export) |

The pre-0.10 un-prefixed names (`suffix`, `ref_parent_traversal_depth`, ...)
were deprecated through the 0.10 series and removed in 0.11.

Example `pyproject.toml`:

```toml
[tool.pytest.ini_options]
httpchain_suffix = "http"
httpchain_ref_parent_traversal_depth = 3
httpchain_max_comprehension_length = 50000
httpchain_max_parallel_iterations = 10000
```

### HAR export

Pass `--httpchain-output-dir DIR` on the pytest command line to write an [HAR](http://www.softwareishard.com/blog/har-12-spec/) file capturing the HTTP exchanges of each test stage:

```bash
pytest --httpchain-output-dir ./har-output
```

Each test that performs a request writes a `.har` file under `DIR` (named from the test node id) and a "HAR File" section is added to that test's report.

A followed redirect adds an entry per hop, each with the body that request carried: a `302` or `303`, and a `301` answering a `POST`, follows up with a bodiless `GET` (a `HEAD` stays a `HEAD`); any other redirect repeats the method and re-sends the original body. When a later hop fails — its target refuses the connection or times out, or the chain exceeds the scenario's redirect limit (`client.max_redirects`, 20 by default) — httpx reports none of the hops before it, so the chain is recorded as a single entry: the failed request.

A multipart (`multipart` or `files`) body is recorded as sent, the boundaries between its parts included: `postData.text` is base64-encoded (`"encoding": "base64"`) when a part is binary.

!!! warning
    HAR files contain full requests and responses, **including credential headers and saved tokens**: a HAR is usually replayed, which needs the real values, so nothing in it is redacted unless `httpchain_har_redact` is on, and bodies never are. Scrub or avoid uploading them as CI artifacts. See [Secrets and sensitive output](advanced/context-layering.md#secrets-and-sensitive-output).

With `httpchain_har_redact = true`, the [report's redaction](#secrets-in-reports) applies to each entry's `url`, `headers`, `cookies`, `queryString` and `redirectURL` too. Names stay, so the HAR still shows which credentials were sent, and `headersSize` stays the size that went on the wire.

### Secrets in reports

When a stage fails, its report shows the HTTP request and response it sent and got. The values of credential headers and of credential query parameters print as `[REDACTED]`, so a token does not end up in CI logs; names stay visible:

```text
GET https://api.example.com/users?page=2&access_token=[REDACTED]
authorization: [REDACTED]
cookie: session=[REDACTED]; theme=[REDACTED]
```

A `Cookie` keeps each cookie's name and a `Set-Cookie` its cookie's name and attributes (`session=[REDACTED]; Path=/; HttpOnly`); any other listed header is hidden whole. The same rules cover:

-   every URL in the report: the request's, and a redirect's `Location` (and `Content-Location`, `Referer`), whose query and `#fragment` parameters are redacted. While `Authorization` is listed, so is a URL's userinfo, which httpx sends in that header: the password of `https://user:password@host`, or the user name of `https://<token>@host`, where it is the whole credential;
-   the `HTTP Request (curl)` section, which gives the request as a curl command to [send it again](troubleshooting.md#sending-the-failing-request-again): a hidden value stays `[REDACTED]` there, and a comment above the command says to fill it in;
-   header checks' failure messages: `Header 'Set-Cookie' doesn't match: expected session=[REDACTED]; Path=/, got session=[REDACTED]; Path=/admin`. The expected value of an exact match is a value of that header, so it is hidden too. A failed `not_contains` or `not_matches` found its operand in the value, so the operand is shown only when the redacted value already shows its text (`contains 'Path=/admin'`) and as `[REDACTED]` otherwise (`contains '[REDACTED]' while it shouldn't` for the old session of a logout check); a failed `contains`/`matches` operand is not in the value and is shown as written;
-   a request error that quotes a header value: `HTTP request failed: Illegal header value b'[REDACTED]'` when a token read from a file kept its trailing newline;
-   the `HTTP Request: GET <url>` line httpx logs at INFO, which a run capturing INFO logs prints with the report.

Request and response **bodies are not redacted**: a form's `password=...`, or a login response's token, shows as sent. Nor is anything logged at DEBUG: the context dumps the plugin logs there hold saved values such as tokens, and httpcore's connection trace lists every response header as received, `Set-Cookie` included. Do not capture DEBUG logs in a CI run against real credentials.

Each list replaces its default, so add the names you need to the ones you keep. Names are separated by whitespace, commas or line breaks, or given as a TOML list:

```toml
[tool.pytest.ini_options]
httpchain_redact_headers = ["Authorization", "Cookie", "Set-Cookie", "X-Tenant-Secret"]
httpchain_redact_query_params = ["access_token", "sig"]
```

An empty value disables that list, for instance to see the real values while debugging locally:

```bash
pytest -o httpchain_redact_headers= -o httpchain_redact_query_params=
```

## IDE Support

pytest-httpchain provides a JSON Schema for test files, enabling autocomplete and validation in your IDE.

### VS Code

Add to your `.vscode/settings.json`:

```json
{
    "json.schemas": [
        {
            "fileMatch": ["**/test_*.http.json"],
            "url": "https://aeresov.github.io/pytest-httpchain/schema/scenario.schema.json"
        }
    ]
}
```

Or reference the schema directly in your test files:

```json
{
    "$schema": "https://aeresov.github.io/pytest-httpchain/schema/scenario.schema.json",
    "stages": [...]
}
```

The `$schema` key is editor metadata — the plugin discards it during validation, even though unknown keys are otherwise rejected.

### JetBrains IDEs

Go to **Settings → Languages & Frameworks → Schemas and DTDs → JSON Schema Mappings** and add a mapping for `**/test_*.http.json` files, pointing to `https://aeresov.github.io/pytest-httpchain/schema/scenario.schema.json`.

## Your First Test

Create a test file named `test_example.http.json`:

```json
{
    "stages": [
        {
            "name": "health check",
            "request": {
                "url": "https://httpbin.org/get"
            },
            "response": [
                {
                    "verify": {
                        "status": 200
                    }
                }
            ]
        }
    ]
}
```

Run with pytest:

```bash
pytest test_example.http.json -v
```

## Basic Concepts

### Scenarios and Stages

A **scenario** is a JSON file containing one or more **stages**. Each stage represents a single HTTP request and its expected response handling.

### Common Data Context

pytest-httpchain maintains a key-value store throughout scenario execution. This context is populated by:

-   Variables defined in `substitutions`
-   pytest fixtures
-   Values saved from responses

Use Jinja-style `{{ expression }}` syntax to reference context values in any request value. Only values are substituted — a template in a dict *key* is sent literally, and is reported as `HTTPCHAIN029`.

### Execution Flow

1. Scenario-level substitutions are processed
2. Stages execute in order
3. Each stage:
    - Processes stage-level substitutions
    - Skips the stage if its `skip_if` holds, leaving the chain running
    - Renders template expressions
    - Executes the HTTP request
    - Processes response steps (verify/save)
4. If a stage fails, remaining stages are skipped (unless `always_run: true`)
