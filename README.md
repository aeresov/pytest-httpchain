# pytest-http

A pytest plugin for HTTP testing using JSON files. Write your HTTP tests in JSON format and let pytest execute them as regular test functions.

## Overview

`pytest-http` allows you to write HTTP integration tests using structured JSON files instead of Python code. It provides:

- **Declarative HTTP testing** - Define requests, responses, and validations in JSON
- **Multi-stage scenarios** - Chain multiple HTTP requests with variable passing between stages
- **pytest integration** - Full pytest features including fixtures, marks, and test discovery
- **Variable substitution** - Use data obtained from previous requests. Jinja2 syntax is supported.
- **JMESPath support** - Extract and validate data from JSON responses
- **User function integration** - Call custom Python functions for complex logic

## Installation

Install normally via package manager of your choice from PyPi.

```bash
pip install pytest-http
```

### Optional dependencies

The following optional dependencies are available:
* `aws`: AWS SigV4 authentication via [requests-auth-aws-sigv4](https://github.com/andrewjroth/requests-auth-aws-sigv4). Details in [AWS Authentication](#aws-authentication).
* `mcp`: installs MCP server package and its starting script. Details in [MCP Server](#mcp-server).

```bash
pip install pytest-http[aws,mcp]
```

### Install from repository

Directly from Github, in case you need a particular ref:

```bash
pip install 'git+https://github.com/aeresov/pytest-http@main'
```

## Quick Start

Create a JSON test file following the pattern `test_<name>.<suffix>.json` (default suffix is `http`):

```json
{
    "flow": [
        {
            "name": "get_user",
            "request": {
                "url": "https://api.example.com/users/1",
                "method": "GET"
            },
            "response": {
                "verify": {
                    "status": 200
                },
                "save": {
                    "vars": {
                        "user_id": "id",
                        "username": "name"
                    }
                }
            }
        },
        {
            "name": "update_user",
            "request": {
                "url": "https://api.example.com/users/{{ user_id }}",
                "method": "PUT",
                "json": {
                    "name": "{{ username }}_updated"
                }
            },
            "response": {
                "verify": {
                    "status": 200
                }
            }
        }
    ]
}
```

Your file will be automatically discovered and treated like a test function. Run with pytest:

```bash
pytest test_api.http.json
```

## JSON Test File Structure

### Root Level Schema

```json
{
    "fixtures": ["fixture1", "fixture2"],
    "marks": ["skip", "xfail(reason='...')"],
    "flow": [...],
    "final": [...]
}
```

- **`fixtures`**: Optional - pytest fixtures to inject
- **`marks`**: Optional - pytest marks  
- **`flow`**: Required - main test stages
- **`final`**: Optional - cleanup stages (always run)

### Stage Schema

Each stage represents one HTTP request-response cycle:

```json
{
    "name": "stage_name",
    "request": {
        "url": "https://api.example.com/endpoint",
        "method": "GET",
        "params": {"key": "value"},
        "headers": {"Authorization": "Bearer token"},
        "json": {"data": "value"}
    },
    "response": {
        "save": {
            "vars": {
                "variable_name": "response.field"
            },
            "functions": [
                "module:function_name",
                {
                    "function": "module:function_name",
                    "kwargs": {"param": "value"}
                }
            ]
        },
        "verify": {
            "status": 200,
            "vars": {
                "saved_variable": "expected_value"
            },
            "functions": ["module:function_name"]
        }
    }
}
```

**Request fields:**
- **`name`**: Required - descriptive name
- **`url`**: Required - endpoint URL
- **`method`**: Optional - HTTP method (defaults to GET)
- **`params`**: Optional - query parameters
- **`headers`**: Optional - HTTP headers
- **`json`**: Optional - JSON body

**Response fields:**
- **`response`**: Optional - response handling configuration
- **`save.vars`**: JMESPath expressions to extract data
- **`save.functions`**: User functions to extract data
- **`verify.status`**: Expected HTTP status code
- **`verify.vars`**: direct assertions for variables
- **`verify.functions`**: User functions for non-trivial assertions

## Key Features

### Saving data for further use

A stage can save data from HTTP response for further use.
This data context is maintained for the whole length of scenario, updated by each subsequent stage.

#### JMESPath Expressions

Extract data from JSON responses directly using JMESPath:

```json
{
    "response": {
        "save": {
            "vars": {
                "user_id": "data.user.id",
                "first_name": "data.user.profile.firstName",
                "item_count": "length(data.items)"
            }
        }
    }
}
```

#### User Functions

Extract data from Response object by calling your own function:

```python
# my_functions.py
import requests
from typing import Any

def simple_extraction(response: requests.Response) -> dict[str, Any]:
    return {"processed_data": response.json()["data"]}

def complex_extraction(response: requests.Response, threshold: int) -> dict[str, Any]:
    return {"activated": len(response.json()["datapoints"]) > threshold}
```

```json
{
    "response": {
        "save": {
            "functions": [
                "my_functions:simple_extraction",
                {
                    "function": "my_functions:complex_extraction",
                    "kwargs": {"threshold": 5}
                }
            ]
        }
    }
}
```

### AWS Authentication

`pytest-http` supports AWS SigV4 authentication for calling AWS APIs. You can use either profile-based or credential-based authentication.

#### Profile-based Authentication

Use AWS profiles from your local AWS configuration:

```json
{
    "aws": {
        "service": "execute-api",
        "region": "us-west-2",
        "profile": "dev"
    },
    "flow": [
        {
            "name": "call_api_gateway",
            "request": {
                "url": "https://api.example.com/prod/endpoint"
            }
        }
    ]
}
```

#### Credential-based Authentication

Use AWS access keys directly:

```json
{
    "aws": {
        "service": "s3",
        "region": "us-east-1",
        "access_key_id": "AKIAIOSFODNN7EXAMPLE",
        "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "session_token": "optional-session-token"
    },
    "flow": [
        {
            "name": "call_s3_api",
            "request": {
                "url": "https://s3.amazonaws.com/my-bucket/object"
            }
        }
    ]
}
```

#### Environment Variables

AWS authentication fields default to standard environment variables:

- `AWS_PROFILE` → `profile`
- `AWS_ACCESS_KEY_ID` → `access_key_id`
- `AWS_SECRET_ACCESS_KEY` → `secret_access_key`
- `AWS_SESSION_TOKEN` → `session_token`
- `AWS_DEFAULT_REGION` → `region`

This allows you to omit credentials from JSON files:

```json
{
    "aws": {
        "service": "execute-api"
    },
    "flow": [...]
}
```

#### Required Fields

- **`service`**: AWS service name (e.g., `execute-api`, `s3`, `lambda`)

### Variable Substitution

The plugin uses **Jinja2 templates** for powerful variable substitution, supporting complex data access patterns. Note that every string is treated as independent Jinja2 environment. This means, while you can use Jinja2 syntax within a string, you can't make the whole JSON file a Jinja2 template, it'll break validation.

#### Basic Variable Access
```json
{
    "request": {
        "url": "https://api.example.com/users/{{ user_id }}",
        "headers": {"Authorization": "Bearer {{ auth_token }}"}
    }
}
```

#### Object Dot Notation
Access nested object properties using dot notation:
```json
{
    "request": {
        "url": "https://api.example.com/users/{{ user.profile.id }}",
        "headers": {"X-User-Role": "{{ user.permissions.role }}"}
    }
}
```

#### Array/List Access
Access array elements using square brackets:
```json
{
    "request": {
        "url": "https://api.example.com/items/{{ items[0] }}/details",
        "json": {"categories": "{{ categories[1] }}"}
    }
}
```

#### Complex Nested Access
Combine dot notation and array access for complex data structures:
```json
{
    "request": {
        "url": "https://api.example.com/users/{{ data.users[0].profile.id }}",
        "json": {
            "primary_address": "{{ user.addresses[0].street }}",
            "backup_email": "{{ user.contacts.emails[1] }}"
        }
    }
}
```

### Using saved data

#### Shaping further stages

Use saved data from previous stages to alter URLs, query parameters, headers etc.

```json
{
    "flow": [
        {
            "name": "login",
            "request": {"url": "/auth"},
            "response": {
                "save": {"vars": {"token": "access_token"}}
            }
        },
        {
            "name": "api_call",
            "request": {
                "url": "/api/data",
                "headers": {"Authorization": "Bearer {{ token }}"}
            }
        }
    ]
}
```

#### Verification

**verify** block acts like assertions in test function. Check saved values directly or call custom functions. You can use saved data from previous stages and from current one.

```python
# my_functions.py
import requests
from typing import Any

def simple_verification(response: requests.Response) -> bool:
    return len(response.json()["datapoints"]) > 0

def complex_verification(response: requests.Response, threshold: int) -> bool:
    return len(response.json()["datapoints"]) > threshold
```

```json
{
    "response": {
        "save": {
            "vars": {
                "user_id": "data.user.id",
                "first_name": "data.user.profile.firstName",
                "item_count": "length(data.items)"
            }
        },
        "verify": {
            "vars": {
                "user_id": 42
            },
            "functions": [
                "my_functions:simple_verification",
                {
                    "function": "my_functions:complex_verification",
                    "kwargs": {"threshold": 5}
                }
            ]
        }
    }
}
```

### JSON References ($ref)

Reuse common pieces across multiple test files. Common **$ref** syntax is supported. Useful for boilerplate, e.g. authentication calls.

**stage_common.json:**
```json
{
    "authenticate": {
        "name": "auth",
        "request": {"url": "/auth/login"},
        "response": {"save": {"vars": {"token": "access_token"}}}
    }
}
```

**test_api.http.json:**
```json
{
    "flow": [
        {"$ref": "stage_common.json#/authenticate"},
        {
            "name": "get_data",
            "request": {
                "url": "/data",
                "headers": {"Authorization": "Bearer {{ token }}"}
            }
        }
    ]
}
```

### Pytest Integration

#### Fixtures

Use pytest fixtures in your JSON tests:

```json
{
    "fixtures": ["server", "auth_token"],
    "flow": [
        {
            "name": "test_with_fixtures",
            "request": {"url": "http://{{ server }}/api/{{ auth_token }}"}
        }
    ]
}
```

#### Marks

Apply pytest marks:

```json
{
    "marks": [
        "skip(reason='API not ready')",
        "xfail"
    ]
}
```

Note: The following markers are **not supported**: `skipif`, `usefixture`, and `parametrize`.

## File Naming Convention

Test files must follow the pattern: `test_<name>.<suffix>.json`

- **Prefix**: Must start with `test_`
- **Name**: Descriptive test name
- **Suffix**: Configurable (default: `http`)
- **Extension**: Must be `.json`

Examples:
- `test_user_api.http.json`
- `test_authentication.http.json`
- `test_payment_flow.http.json`

Configure custom suffix in `pytest.ini`:

```ini
[pytest]
suffix = rest
```

## Project Structure

```
pytest_http/
├── __init__.py
├── pytest_plugin.py    # Main pytest plugin implementation
├── models.py           # Pydantic models for JSON validation
├── types.py           # Type definitions and validators
└── user_function.py   # User function handling

tests/
├── unit/              # Unit tests for the plugin
└── integration/       # Integration tests
    └── examples/      # Example JSON test files
        ├── test_full.http.json
        ├── test_mark_xfail.http.json
        └── stage_ref.json
```

## Development

Python tooling used in this project:

- **Package manager**: `uv`
- **Linting/formatting**: `ruff`
- **Testing**: `pytest`

### Commands

```bash
# Install dependencies
uv sync

# Run tests
uv run pytest

# Lint and format
uv run ruff check
uv run ruff format

# Run specific test
uv run pytest tests/integration/examples/test_full.http.json
```

### Key dependencies

- **pytest**: Test framework integration
- **jsonref**: JSON reference resolution
- **pydantic**: Data validation and parsing
- **jmespath**: JSON query language for response data extraction
- **jinja2**: Template engine for variable substitution
- **requests**: HTTP client

## Configuration

### pytest.ini

```ini
[pytest]
# Custom file suffix (default: http)
suffix = rest
```

### pyproject.toml

```toml
[tool.pytest.ini_options]
suffix = rest
```

## Error Handling

Exceptions to be expected from this plugin:

- **JSON validation errors**: Invalid test file syntax
- **Pydantic validation errors**: Invalid test file structure
- **Variable substitution errors**: Missing or invalid variables
- **HTTP request errors**: Network or server issues
- **JMESPath errors**: Invalid query expressions
- **Function import errors**: Missing or invalid user functions

## Best Practices

- **Descriptive stage names**: Use clear, action-oriented names
- **Modular design**: Use `$ref` for reusable components
- **Cleanup**: Use `final` stages for cleanup operations
- **Fixtures**: Leverage pytest fixtures for test data and setup

## MCP Server

`pytest-http` includes an MCP (Model Context Protocol) server that provides JSON validation and schema support for AI assistants working with HTTP test files.

### Installation

The optional dependency `mcp` installs MCP server's package.

The script `pytest-http-mcp` gets installed automatically. Use it as a call target for your MCP configuration.

```json
// .mcp.json for Claude Code
{
  "mcpServers": {
    "pytest-http": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run",
        "pytest-http-mcp"
      ],
      "env": {}
    }
  }
}
```

### Features

The MCP server provides:

- **JSON Validation**: Verify HTTP test JSON files against the Scenario schema
- **Schema Export**: Access the complete JSON schema for test file structure
- **Example Generation**: Get complete example test scenarios


## Examples

See the `tests/integration/examples/` directory for complete working examples of:

- Basic HTTP requests and responses
- Multi-stage scenarios with variable passing
- pytest mark usage
- JSON reference usage
- Custom function integration
