# Troubleshooting

## Test Files Not Discovered

Ensure your test files follow the naming pattern `test_<name>.<suffix>.json` where the suffix (`httpchain_suffix` ini option) defaults to `http`.

**Checklist:**

-   File name starts with `test_`
-   File name contains the suffix (default: `.http.`)
-   File extension is `.json`
-   Check `pytest.ini` if you've customized the suffix

**Example valid names:**

-   `test_api.http.json` (default suffix)
-   `test_users.http.json`
-   `test_auth.api.json` (if suffix configured as `api`)

## Reference Resolution Fails (`$include` / `$merge` / `$ref`)

All three directives (`$include`, `$merge`, `$ref`) work identically. Use `$include` or `$merge` to avoid VS Code/IDE validation conflicts.

### VS Code Shows Validation Errors for `$ref`

VS Code treats `$ref` as a JSON Schema keyword and may show spurious errors. Use `$include` or `$merge` instead:

```json
// Use these - no IDE conflicts
{ "$include": "common/auth.json#/login_stage" }
{ "$merge": "base.json", "extra_key": "value" }

// Instead of this - may show VS Code errors
{ "$ref": "common/auth.json#/login_stage" }
```

Sibling keys next to `$merge` are merged **additively** — they can add new keys,
but they do not override existing ones. Supplying a key that already exists in the
merged document with a different scalar value raises a `Merge conflict`, not a
silent override.

### Path Issues

-   Verify the referenced file path is correct (relative to the referencing file)
-   Check that parent directory traversal doesn't exceed `httpchain_ref_parent_traversal_depth` (default: 3)
-   Use forward slashes `/` even on Windows

### JSON Pointer Issues

-   Ensure the JSON pointer (e.g., `#/path/to/key`) points to an existing key
-   Keys are case-sensitive
-   Array indices are zero-based: `#/stages/0` for first stage

**Example:**

```json
{
    "$include": "common/auth.json#/login_stage"
}
```

This references the `login_stage` key in `common/auth.json` relative to the current file.

## Template Expression Errors

### Variable Not Found

-   Ensure variables are defined in `substitutions` before use
-   Check that fixtures are listed in the `fixtures` array
-   Variables from `save` steps are only available in subsequent stages

You do not have to check these by hand: `pytest-httpchain validate` reports an
undefined name as [`HTTPCHAIN003`](diagnostics.md) and a name used before it is
saved as `HTTPCHAIN004`, naming the stage *and* the phase it appears in.
`pytest-httpchain show` prints where each consumed variable comes from — see the
[CLI reference](cli.md).

### Syntax Errors

-   Template expressions use Python syntax inside `{{ }}`
-   Check for typos in variable names
-   Ensure quotes are balanced

**Valid expressions:**

```json
"{{ user_id }}"
"{{ user_id + 1 }}"
"{{ str(timestamp()) }}"
"{{ 'prefix_' + name }}"
```

### "was declared as ... but rendered to None"

A setting or check — a header matcher field, `verify.status`, a
`verify.jmespath` matcher operand, `parallel.calls_per_sec`, `request.auth`,
`ssl.cert`, `url`, ... — was written as a template that rendered to `null`,
typically `get()` without a default or a JMESPath save of a key the response
did not have. Fix where the value comes from, or give `get()` a default; where
a `verify.jmespath` operand was meant to be `null`, write `null`. See
[Templates that render to `null`](usage/substitutions.md#templates-that-render-to-null).

### Comprehension Limits

If you hit `MAX_COMPREHENSION_LENGTH` errors, either:

-   Simplify your expression
-   Increase `httpchain_max_comprehension_length` in pytest config

## Stage Execution Stops Unexpectedly

### Chain Behavior

One stage failure stops the entire chain by default. This is intentional to prevent cascading failures.

**Solutions:**

-   Use `always_run: true` for cleanup stages that must execute regardless of prior failures
-   Check test output for specific error messages from failed verifications

### Verification Failures

A failing verify step lists every check that failed, numbered in the order the
checks ran, so one run shows all that is wrong with the response; a single
failure is its message alone:

```text
3 verification checks failed:
  1. Status code doesn't match: expected 201, got 200
  2. Header 'Content-Type' (value: 'text/html') doesn't contain 'json'
  3. JMESPath 'data.id' doesn't match: expected 42, got null
```

A template in the step that cannot be rendered is listed in its check's place,
in the template engine's words (`KeyError in expression '{{ response.headers['x-missing'] == 'a' }}': 'x-missing'`),
and only that check does not run. A user function's `pytest.skip()` or
`pytest.xfail()` does not skip a stage whose step already has such a failure,
or a check that failed before the function ran, or a template that calls
`pytest.fail()`: the stage fails with them. User functions run after a check
in their step failed, so one that assumes the response is good (calling
`response.json()` on an HTML error page) adds its own error to the list; see
[User Function Verification](usage/responses.md#user-function-verification).

Only that step's checks are listed. The stage ends at the first step that
fails, so the checks of a later verify step have not run: they may show
failures of their own once these are fixed. See
[Verify Steps](usage/responses.md#verify-steps).

Common causes:

-   Status code mismatch
-   Missing or incorrect response headers
-   JMESPath expression returns `null` instead of expected value
-   A `verify.jmespath` value equal in Python but not in JSON: `expected 1, got true`, `expected "42", got 42` (see [JMESPath Assertions](usage/responses.md#jmespath-assertions))
-   JSON Schema validation failure
-   A body that is not JSON (`Cannot check verify.jmespath, response is not valid JSON`), listed once however many checks wanted it: often an HTML error page, which the `HTTP Response` section shows
-   A header or `jmespath` matcher's `matches`/`not_matches` that a template rendered to text that is not a regular expression (`matches must resolve to a regular expression, got '{{ ( }}' (missing ), ...)`), or to one too big for Python's `re` to compile (`the repetition number is too large`): the template's value, often saved from a response, is used as a pattern as it is

### Sending the Failing Request Again

Below the failure message, the report shows the stage's request and response.
Its `HTTP Request (curl)` section holds the same request as a curl command, to
send it again from a POSIX shell such as bash. The `#` note lines above the
command are comments to bash, but not to an interactive zsh, which takes them
as commands unless `setopt interactivecomments` is set: there, copy the command
without them.

```text
# [REDACTED] stands for a value this report hides: fill it in before running.
curl -X POST 'https://api.example.com/users?access_token=[REDACTED]' \
  --globoff \
  -H 'accept: */*' \
  -H 'user-agent: python-httpx/0.28.1' \
  -H 'authorization: [REDACTED]' \
  -H 'content-type: application/json' \
  --compressed \
  --data-raw '{"name":"Alice"}'
```

-   Values the report [redacts](#redacted-in-a-report) stay `[REDACTED]`, and the comment says so: put the real ones in, or switch redaction off for a local run.
-   Every header the request carried is sent, bar those curl writes itself: `Host` (unless the request set its own), `Content-Length` (unless there is no body: a bodyless `POST`'s `Content-Length: 0` is kept, as curl would send none), `Transfer-Encoding` and `Connection`. The `Accept-Encoding` httpx sends on its own (`gzip, deflate`, with `br` and `zstd` where their decoders are installed) becomes `--compressed`, which asks for the encodings curl decodes; one the request set itself (`identity`, `br` alone) is kept, and curl sends it in place of its own. `--compressed` is there either way, so that curl decodes a compressed response, as httpx does whatever it asked for. A URL holding `[`, `]`, `{` or `}` (a `[REDACTED]` value, a `filter[id]` parameter) gets `--globoff`, or curl would read them as ranges of URLs to request.
-   A textual body is given as sent, quoted for the shell (a JSON body is not pretty-printed as the `HTTP Request` section shows it). A binary body, or one over 10,000 characters, is read from a file instead (`--data-binary @body.bin`, `@body.txt`), which the comment asks you to create: the [HAR export](getting-started.md#har-export) holds the body (base64 for a binary one). A multipart (`files`) upload's body is not captured, so the comment says to add its parts with `-F`.
-   The scenario's `ssl` settings are not part of the request, so the command has none: add `-k` for `"verify": false`, `--cacert` for a CA bundle, `--cert`/`--key` for a client certificate. Nor does it pin the HTTP version; curl negotiates its own.
-   Nor is the scenario's `client.proxy`: the command connects directly, or through the proxy curl's own environment variables (`https_proxy`, `http_proxy`) name. For an API reachable only through the proxy, add `-x <proxy url>`.
-   A [digest-authenticated](usage/requests.md#authentication) request's `Authorization` answered one challenge, with its one-time nonce, so it cannot be sent again: the command leaves it out, and the comment says to add `--digest -u 'user:password'`, which answers a new challenge. A basic or bearer `Authorization` is sent (as `[REDACTED]` to fill in, while redacted).
-   For a parallel stage or a followed redirect, the command is for the request the section's title names (`(failing of 3 parallel iterations)`, `(after 1 redirect)`), like the `HTTP Request` section's.

### `[REDACTED]` in a Report

The values of credential headers and query parameters are hidden in report sections and in header checks' failure messages (see [Secrets in reports](getting-started.md#secrets-in-reports)). A failed exact match on such a header can then read `expected session=[REDACTED]; Path=/, got session=[REDACTED]; Path=/`: the values differ, both are hidden. Likewise `contains '[REDACTED]' while it shouldn't` is a `not_contains` operand found in the hidden part of the value, and `Illegal header value b'[REDACTED]'` a header value the HTTP library refused, usually for a trailing newline in a token read from a file. To see them while debugging locally, switch redaction off for the run:

```bash
pytest -o httpchain_redact_headers= -o httpchain_redact_query_params=
```

## HTTP Request Errors

### Connection Errors

-   Verify the target server is running
-   Check URL for typos
-   Ensure network connectivity

### "Request URL ... is relative"

A URL without a scheme (`/users/1`) is relative to the scenario's `client.base_url`, and there is
none. Set `base_url` in the scenario's [`client` block](usage/scenarios.md#client-configuration), or
make the URL absolute. A literal relative URL fails collection with `HTTPCHAIN034`; one a template
rendered fails its stage. With a `base_url`, remember that httpx appends the URL to the base
URL's path: with `https://api.example.com/v1`, `/users/1` requests `/v1/users/1`, not `/users/1`.

### SSL/TLS Errors

Use SSL configuration to handle certificate issues:

```json
{
    "ssl": {
        "verify": false
    },
    "stages": [...]
}
```

Or specify a custom CA bundle:

```json
{
    "ssl": {
        "verify": "/path/to/ca-bundle.crt"
    },
    "stages": [...]
}
```

### Timeout Errors

Increase the timeout for slow endpoints (or for every stage of a scenario, with
[`client.timeout`](usage/scenarios.md#client-configuration)):

```json
{
    "request": {
        "url": "https://slow-api.example.com/endpoint",
        "timeout": 60.0
    }
}
```

## User Function Errors

### Import Failures

-   Verify the module path uses dot notation: `mypackage.module:function`
-   Ensure the module is importable (in `PYTHONPATH` or installed)
-   Check for syntax errors in the module

### Function Signature Errors

Save functions must accept `httpx.Response` and return `dict[str, Any]`:

```python
def my_save(response: httpx.Response) -> dict[str, Any]:
    return {"key": response.json()["value"]}
```

Verify functions must accept `httpx.Response` and return `bool`:

```python
def my_verify(response: httpx.Response) -> bool:
    return response.json()["status"] == "ok"
```
