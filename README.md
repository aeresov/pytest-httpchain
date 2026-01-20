[![image](https://img.shields.io/pypi/v/pytest-httpchain)](https://pypi.python.org/pypi/pytest-httpchain)
[![image](https://img.shields.io/pypi/l/pytest-httpchain)](https://github.com/aeresov/pytest-httpchain/blob/main/LICENSE)
[![image](https://img.shields.io/pypi/pyversions/pytest-httpchain)](https://pypi.python.org/pypi/pytest-httpchain)

# pytest-httpchain

A pytest plugin for testing HTTP endpoints.

## Overview

`pytest-httpchain` is an integration testing framework for HTTP APIs based on [httpx](https://www.python-httpx.org) lib.  
It aims at helping with common HTTP API testing scenarios, where user needs to make several calls in specific order using data obtained along the way, like auth tokens or resource ids.

## Why pytest-httpchain?

Testing HTTP APIs with plain pytest often leads to these pain points:

-   **Boilerplate accumulates** — Every test repeats the same setup: create client, set headers, make request, parse response, assert. The actual test intent gets buried.
-   **Data threading is manual** — When one call returns a token or ID needed by the next, you end up with fragile helper functions passing state around.
-   **Common patterns get copy-pasted** — Auth flows, base URLs, shared headers end up duplicated across test files. Fixtures might help, but they are not designed for that.
-   **Code reviews are noisy** — The actual test logic is rarely clear because of all the boilerplate and helpers, following changes gets overwhelming quickly.

`pytest-httpchain` offers a more structured approach.

## Features

### Declarative JSON format

Test scenarios are JSON documents that describe _what_ to test, not _how_. No setup code to scroll through — the request and assertions are right there.

### `$ref` with deep merging

Reuse arbitrary parts of your scenarios with JSONRef. Properties merge with type checking, so you can compose scenarios from shared fragments (auth flows, common headers, base URLs).

### Multi-stage execution

Each scenario contains 1+ stages executed in order. One stage failure stops the chain. Use `always_run` for cleanup stages that should execute regardless.

### Common data context

A key-value store persists throughout scenario execution. Variables, fixtures, and saved response data all live here. Use Jinja-style expressions (`{{ var }}`) anywhere in your requests — substitution happens dynamically before each stage.

### Response processing

-   **JMESPath** — Extract values from JSON responses directly
-   **JSON Schema** — Validate response structure against a schema
-   **User functions** — Call Python functions for custom extraction, verification, or [authentication](https://requests.readthedocs.io/en/latest/user/advanced/#custom-authentication)

### Full pytest integration

Markers, fixtures, parametrization, and other plugins work as expected. You're not locked into a separate ecosystem.

## Quick Start

Create a JSON test file named like `test_<name>.<suffix>.json` (default suffix is `http`):

```python
# conftest.py
import pytest
from datetime import datetime

@pytest.fixture
def now_utc():
    return datetime.now()
```

```json
{
    "substitutions": [
        {
            "vars": {
                "user_id": 1
            }
        }
    ],
    "stages": {
        "get_user": {
            "request": {
                "url": "https://api.example.com/users/{{ user_id }}"
            },
            "response": [
                {
                    "verify": {
                        "status": 200
                    }
                },
                {
                    "save": {
                        "jmespath": {
                            "user_name": "user.name"
                        }
                    }
                }
            ]
        },
        "update_user": {
            "fixtures": ["now_utc"],
            "request": {
                "url": "https://api.example.com/users/{{ user_id }}",
                "method": "PUT",
                "body": {
                    "json": {
                        "user": {
                            "name": "{{ user_name }}_updated",
                            "timestamp": "{{ str(now_utc) }}"
                        }
                    }
                }
            },
            "response": [
                {
                    "verify": {
                        "status": 200
                    }
                }
            ]
        },
        "cleanup": {
            "always_run": true,
            "request": {
                "url": "https://api.example.com/cleanup",
                "method": "POST"
            }
        }
    }
}
```

Scenario we created:

-   common data context is seeded with the first variable `user_id`
-   **get_user**  
    url is assembled using `user_id` variable from common data context  
    HTTP GET call is made  
    we verify the call returned code 200  
    assuming JSON body is returned, we extract a value by JMESPath expression `user.name` and save it to common data context under `user_name` key
-   **update_user**  
    `now_utc` fixture value is injected into common data context  
    url is assembled using `user_id` variable from common data context  
    we create JSON body in place using values from common data context, note that `now_utc` is converted to string in place  
    HTTP PUT call with body is made  
    we verify the call returned code 200
-   **cleanup**  
    finalizing call meant for graceful exit  
    `always_run` parameter means this stage will be executed regardless of errors in previous stages

For detailed usage guide see the [full documentation](https://aeresov.github.io/pytest-httpchain).

## Installation

Install normally via package manager of your choice from PyPi:

```bash
pip install pytest-httpchain
```

or directly from Github, in case you need a particular ref:

```bash
pip install 'git+https://github.com/aeresov/pytest-httpchain@main'
```

### Optional dependencies

The following optional dependencies are available:

-   `mcp`: installs MCP server package and its starting script. Details in [MCP Server](#mcp-server).

## Configuration

-   Test file discovery is based on this name pattern: `test_<name>.<suffix>.json`.
    The `suffix` is configurable as pytest ini option, default value is **http**.
-   `$ref` instructions can point to other files; absolute and relative paths are supported.
    You can limit the depth of relative path traversal using `ref_parent_traversal_depth` ini option, default value is **3**.
-   Template expressions support list/dict comprehensions. You can limit the maximum comprehension length using `max_comprehension_length` ini option, default value is **50000**.

## MCP Server

`pytest-httpchain` includes an MCP (Model Context Protocol) server to aid AI code assistants.

### Installation

The optional dependency `mcp` installs MCP server's package and `pytest-httpchain-mcp` script.  
Use this script as call target for your MCP configuration.

Claude Code `.mcp.json` example:

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

### Features

The MCP server provides:

-   **Scenario validation** - validate test scenario and scan for possible problems

## Documentation

-   [Full Documentation](https://aeresov.github.io/pytest-httpchain) - Complete usage guide
-   [Changelog](CHANGELOG.md) - Release notes

## Thanks

This project was inspired by [Tavern](https://github.com/taverntesting/tavern) and [pytest-play](https://github.com/davidemoro/pytest-play).  

[httpx](https://www.python-httpx.org) does comms.  
[Pydantic](https://docs.pydantic.dev) keeps structure.  
[simpleeval](https://github.com/danthedeckie/simpleeval) powers templates.  
[pytest-order](https://github.com/pytest-dev/pytest-order) sorts chain.  
[pytest-datadir](https://github.com/gabrielcnr/pytest-datadir) saved me a lot of elbow grease while testing.
