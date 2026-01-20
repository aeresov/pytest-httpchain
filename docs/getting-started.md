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

### Optional Dependencies

-   `mcp`: Installs the MCP server package for AI assistant integration

```bash
pip install 'pytest-httpchain[mcp]'
```

## Configuration

Configuration options can be set in `pytest.ini` or `pyproject.toml` under `[tool.pytest.ini_options]`.

| Option | Default | Description |
|--------|---------|-------------|
| `suffix` | `http` | File suffix for test discovery. Files must match `test_<name>.<suffix>.json` |
| `ref_parent_traversal_depth` | `3` | Maximum parent directory traversals allowed in `$ref` paths |
| `max_comprehension_length` | `50000` | Maximum length for list/dict comprehensions in template expressions |

Example `pyproject.toml`:

```toml
[tool.pytest.ini_options]
suffix = "http"
ref_parent_traversal_depth = 3
max_comprehension_length = 50000
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

Use Jinja-style `{{ expression }}` syntax to reference context values anywhere in your requests.

### Execution Flow

1. Scenario-level substitutions are processed
2. Stages execute in order
3. Each stage:
    - Processes stage-level substitutions
    - Renders template expressions
    - Executes the HTTP request
    - Processes response steps (verify/save)
4. If a stage fails, remaining stages are skipped (unless `always_run: true`)
