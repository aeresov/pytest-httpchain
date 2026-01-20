[![image](https://img.shields.io/pypi/v/pytest-httpchain)](https://pypi.python.org/pypi/pytest-httpchain)
[![image](https://img.shields.io/pypi/l/pytest-httpchain)](https://github.com/aeresov/pytest-httpchain/blob/main/LICENSE)
[![image](https://img.shields.io/pypi/pyversions/pytest-httpchain)](https://pypi.python.org/pypi/pytest-httpchain)

# pytest-httpchain

A pytest plugin for declarative HTTP API integration testing.

## Overview

`pytest-httpchain` is an integration testing framework for HTTP APIs based on [httpx](https://www.python-httpx.org/).
It helps with common HTTP API testing scenarios where you need to make several calls in specific order using data obtained along the way, like auth tokens or resource IDs.

## Why pytest-httpchain?

Testing HTTP APIs with plain pytest often leads to these pain points:

-   **Boilerplate accumulates** — Every test repeats the same setup: create client, set headers, make request, parse response, assert. The actual test intent gets buried.
-   **Data threading is manual** — When one call returns a token or ID needed by the next, you end up with fragile helper functions passing state around.
-   **Common patterns get copy-pasted** — Auth flows, base URLs, shared headers end up duplicated across test files.
-   **Code reviews are noisy** — The actual test logic is rarely clear because of all the boilerplate.

`pytest-httpchain` offers a more structured approach.

## Features

### Declarative JSON Format

Test scenarios are JSON documents that describe _what_ to test, not _how_. No setup code to scroll through — the request and assertions are right there.

### `$ref` with Deep Merging

Reuse arbitrary parts of your scenarios with JSONRef. Properties merge with type checking, so you can compose scenarios from shared fragments (auth flows, common headers, base URLs).

### Multi-Stage Execution

Each scenario contains 1+ stages executed in order. One stage failure stops the chain. Use `always_run` for cleanup stages that should execute regardless.

### Common Data Context

A key-value store persists throughout scenario execution. Variables, fixtures, and saved response data all live here. Use Jinja-style expressions (`{{ var }}`) anywhere in your requests.

### Response Processing

-   **JMESPath** — Extract values from JSON responses directly
-   **JSON Schema** — Validate response structure against a schema
-   **User functions** — Call Python functions for custom extraction, verification, or authentication

### Parametrization

Run stages with different parameter values, similar to pytest's `@pytest.mark.parametrize`.

### Parallel Execution

Execute multiple requests concurrently for load testing, stress testing, or bulk operations.

### Full pytest Integration

Markers, fixtures, parametrization, and other plugins work as expected. You're not locked into a separate ecosystem.

## Quick Example

```json
{
    "substitutions": [
        {"vars": {"user_id": 1}}
    ],
    "stages": {
        "get_user": {
            "request": {
                "url": "https://api.example.com/users/{{ user_id }}"
            },
            "response": [
                {"verify": {"status": 200}},
                {"save": {"jmespath": {"user_name": "name"}}}
            ]
        },
        "update_user": {
            "request": {
                "url": "https://api.example.com/users/{{ user_id }}",
                "method": "PUT",
                "body": {
                    "json": {"name": "{{ user_name }}_updated"}
                }
            },
            "response": [
                {"verify": {"status": 200}}
            ]
        }
    }
}
```

## Installation

```bash
pip install pytest-httpchain
```

See [Getting Started](getting-started.md) for detailed installation and configuration instructions.

## MCP Server

`pytest-httpchain` includes an MCP (Model Context Protocol) server to aid AI code assistants.

Install with the `mcp` extra:

```bash
pip install 'pytest-httpchain[mcp]'
```

Configure in Claude Code `.mcp.json`:

```json
{
    "mcpServers": {
        "pytest-httpchain": {
            "type": "stdio",
            "command": "uv",
            "args": ["run", "pytest-httpchain-mcp"],
            "env": {}
        }
    }
}
```

## Thanks

This project was inspired by [Tavern](https://github.com/taverntesting/tavern) and [pytest-play](https://github.com/davidemoro/pytest-play).

[httpx](https://www.python-httpx.org) does comms.
[Pydantic](https://docs.pydantic.dev) keeps structure.
[simpleeval](https://github.com/danthedeckie/simpleeval) powers templates.
[pytest-order](https://github.com/pytest-dev/pytest-order) sorts chain.
[pytest-datadir](https://github.com/gabrielcnr/pytest-datadir) saved a lot of elbow grease while testing.
