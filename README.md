# pytest-http

A pytest plugin for HTTP testing using JSON files. Write your HTTP tests in JSON format and let pytest execute them as regular test functions.

## Overview

`pytest-http` allows you to write HTTP integration tests using structured JSON files instead of Python code. It provides:

- **Declarative HTTP testing** - Define requests, responses, and validations in JSON
- **Multi-stage scenarios** - Chain multiple HTTP requests with variable passing between stages
- **pytest integration** - Full pytest features including fixtures, marks, and test discovery
- **Variable substitution** - Use values from previous requests in subsequent ones
- **JMESPath support** - Extract and validate data from JSON responses
- **User function integration** - Call custom Python functions for complex logic

## Installation

```bash
pip install pytest-http
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
                "url": "https://api.example.com/users/{user_id}",
                "method": "PUT",
                "json": {
                    "name": "{username}_updated"
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

Run with pytest:
```bash
pytest test_api.http.json
```

## JSON Test File Structure

### Root Level Schema

```json
{
    "fixtures": ["fixture1", "fixture2"],  // Optional: pytest fixtures to inject
    "marks": ["skip", "xfail(reason='...')"],  // Optional: pytest marks
    "flow": [...],  // Required: main test stages
    "final": [...]  // Optional: cleanup stages (always run)
}
```

### Stage Schema

Each stage represents one HTTP request-response cycle:

```json
{
    "name": "stage_name",  // Required: descriptive name
    "request": {
        "url": "https://api.example.com/endpoint",  // Required
        "method": "GET",  // Optional: defaults to GET
        "params": {"key": "value"},  // Optional: query parameters
        "headers": {"Authorization": "Bearer token"},  // Optional
        "json": {"data": "value"}  // Optional: JSON body
    },
    "response": {  // Optional
        "save": {
            "vars": {
                "variable_name": "response.field"  // JMESPath expressions
            },
            "functions": [
                "module.function_name",  // Function name only
                {
                    "function": "module.function_name",
                    "kwargs": {"param": "value"}
                }
            ]
        },
        "verify": {
            "status": 200,  // Expected HTTP status
            "vars": {
                "saved_variable": "expected_value"
            },
            "functions": ["module.verification_function"]
        }
    }
}
```

## Key Features

### Saving data for further use

Stage can save data from HTTP response for further use.
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

### Using saved data

#### Shaping further stages

Use saved data from previous stages to alter urls, query parameters, headers etc.

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
                "headers": {"Authorization": "Bearer {token}"}
            }
        }
    ]
}
```

#### Verification

**verify** block acts like assertions in test function. You can use saved data from previous stages and from current one.

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

Reuse common stages across multiple test files:

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
                "headers": {"Authorization": "Bearer {token}"}
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
            "request": {"url": "http://{server}/api/{auth_token}"}
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
        "xfail",
        "parametrize('param', [1, 2, 3])"
    ]
}
```

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
suffix = api
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
- **jmespath**: JSON query language
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

## Examples

See the `tests/integration/examples/` directory for complete working examples of:

- Basic HTTP requests and responses
- Multi-stage scenarios with variable passing
- pytest mark usage
- JSON reference usage
- Custom function integration
