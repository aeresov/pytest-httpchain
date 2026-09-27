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
| `httpchain_ref_parent_traversal_depth` | `3` | Maximum parent directory traversals allowed in `$include`/`$merge`/`$ref` paths |
| `httpchain_max_comprehension_length` | `50000` | Maximum length for list/dict comprehensions in template expressions |
| `httpchain_max_parallel_iterations` | `10000` | Maximum number of parallel iterations (`repeat`/`foreach`) allowed per stage |

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

A followed redirect adds an entry per hop, each with the body that request carried: a `302` or `303`, and a `301` answering a `POST`, follows up with a bodiless `GET` (a `HEAD` stays a `HEAD`); any other redirect repeats the method and re-sends the original body. When a later hop fails — its target refuses the connection or times out, or the chain exceeds httpx's limit of 20 redirects — httpx reports none of the hops before it, so the chain is recorded as a single entry: the failed request.

A multipart (`files`) upload's body is not captured: its entry has `bodySize: -1` and no `postData`, and the report's request section says the body was not captured.

!!! warning
    HAR files (and INFO logs and report sections) contain full requests and responses, **including credential headers and saved tokens** — nothing is redacted. Scrub or avoid uploading them as CI artifacts. See [Secrets and sensitive output](advanced/context-layering.md#secrets-and-sensitive-output).

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
    - Renders template expressions
    - Executes the HTTP request
    - Processes response steps (verify/save)
4. If a stage fails, remaining stages are skipped (unless `always_run: true`)
