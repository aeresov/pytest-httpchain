[![image](https://img.shields.io/pypi/v/pytest-http)](https://pypi.python.org/pypi/pytest-http)
[![image](https://img.shields.io/pypi/l/pytest-http)](https://github.com/aeresov/pytest-http/blob/main/LICENSE)
[![image](https://img.shields.io/pypi/pyversions/pytest-http)](https://pypi.python.org/pypi/pytest-http)

# pytest-http

A pytest plugin for testing HTTP endpoints.

## Overview

`pytest-http` aims at helping with common HTTP API testing scenarios, where user needs to make several calls in specific order using data obtained along the way.

For example:
1. **login with test credentials** → (saves: `token`)  
   ↓ (carries: `token`)
2. → (uses: `token`) **create test resource** → (saves: `resource id`)  
   ↓ (carries: `token`, `resource id`)
3. → (uses: `token`, `resource id`) **make test operation, verify result**  
   ↓ (carries: `token`, `resource id`)
4. → (uses: `token`, `resource id`) **delete test resource**  
   ↓ (carries: `token`, `resource id`)
5. → (uses: `token`) **logout**


## Installation

Install normally via package manager of your choice from PyPi.

```bash
pip install pytest-http
```

### Optional dependencies

The following optional dependencies are available:

-   `mcp`: installs MCP server package and its starting script. Details in [MCP Server](#mcp-server).

    ```bash
    pip install pytest-http[mcp]
    ```

### Install from repository

Directly from Github, in case you need a particular ref:

```bash
pip install 'git+https://github.com/aeresov/pytest-http@main'
```

## Features

- **Declarative format** - write tests in JSON
- **Multi-stage tests** - HTTP calls are executed in order; stage failure stops the rest of chain
- **Common data context** - shared data storage for all stages
- **Variable substitution** - use simple Python expressions for injecting data
- **References** - reuse parts of your tests with "$ref" and props merging
- **User functions integration** - use your own code to verify and/or extract data from HTTP response
- **JMESPath support** - extract data directly from JSON responses
- **JSON schema support** - validate response body against your own schema
- **pytest integration** - use the full power of pytest: markers, fixtures, test discovery, other plugins

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
    "vars": {
        "user_id": 1
    },
    "stages": [
        {
            "name": "get_user",
            "request": {
                "url": "https://api.example.com/users/{{ user_id }}"
            },
            "save": {
                "vars": {
                    "user_name": "user.name"
                }
            },
            "verify": {
                "status": 200
            }
        },
        {
            "name": "update_user",
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
            "verify": {
                "status": 200
            }
        },
        {
            "name": "cleanup",
            "always_run": true,
            "request": {
                "url": "https://api.example.com/cleanup",
                "method": "POST"
            }
        }
    ]
}
```

Scenario we created:
- common data context is seeded with the first variable `user_id`
- **get_user** - simple GET call, url is assembled using `user_id` variable from common data context  
    assuming JSON body is returned, we extract user name using JMESPath expression and save into common data context  
    we also verify the call returned code 200
- **update_user** - PUT call with data  
    we create JSON body in place using data saved into common data context during **get_user** stage and `now_utc` pytest fixture converting its value to string in-place  
    we also verify the call returned code 200
- **cleanup** - finalizing call  
    `always_run` parameter means this stage will be executed regardless of errors in previous stages

For detailed examples and usage patterns, see [USAGE.md](USAGE.md).

## Configuration

Basic configuration in `pytest.ini`

```ini
[pytest]
suffix = http
```

or in `pyproject.toml`

```toml
[tool.pytest.ini_options]
suffix = http
```

See [USAGE.md](USAGE.md) for all configuration options and examples.

## Documentation

- [Usage Examples](USAGE.md) - Practical code examples
- [Full Documentation](https://aeresov.github.io/pytest-http) - Complete guide
- [Changelog](CHANGELOG.md) - Release notes
