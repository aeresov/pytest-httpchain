# pytest-http

A pytest plugin for HTTP testing using JSON files. Write your HTTP tests in JSON format and let pytest execute them as regular test functions.

## Overview

`pytest-http` allows you to write HTTP integration tests using structured JSON files instead of Python code. It provides:

-   **Declarative HTTP testing** - Define requests, responses, and validations in JSON
-   **Multi-stage scenarios** - Chain multiple HTTP requests with variable passing between stages
-   **pytest integration** - Full pytest features including fixtures, marks, and test discovery
-   **Variable substitution** - Use data obtained from previous requests. Jinja2 syntax is supported.
-   **JMESPath support** - Extract and validate data from JSON responses
-   **User function integration** - Call custom Python functions for complex logic
-   **Regex pattern matching** - Verify response body content with regular expressions

## Installation

Install normally via package manager of your choice from PyPi.

```bash
pip install pytest-http
```

### Optional dependencies

The following optional dependencies are available:

-   `aws`: AWS SigV4 authentication via [requests-auth-aws-sigv4](https://github.com/andrewjroth/requests-auth-aws-sigv4). Details in [AWS Authentication](#aws-authentication).
-   `mcp`: installs MCP server package and its starting script. Details in [MCP Server](#mcp-server).

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
    "stages": [
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
                "body": {
                    "json": {
                        "name": "{{ username }}_updated"
                    }
                }
            },
            "response": {
                "verify": {
                    "status": 200
                }
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
    "vars": {
        "base_url": "https://api.example.com",
        "api_key": "test-key-123"
    },
    "stages": [...]
}
```

-   **`fixtures`**: Optional - pytest fixtures to inject
-   **`marks`**: Optional - pytest marks
-   **`vars`**: Optional - initial variables for the scenario context
-   **`stages`**: Required - collection of test stages

### Stage Schema

Each stage represents one HTTP request-response cycle:

```json
{
    "name": "stage_name",
    "always_run": false,
    "request": {
        "url": "https://api.example.com/endpoint",
        "method": "GET",
        "params": { "key": "value" },
        "headers": { "Authorization": "Bearer token" },
        "body": {
            "json": { "data": "value" }
        },
        "timeout": 30.0
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
                    "kwargs": { "param": "value" }
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

**Stage fields:**

-   **`name`**: Required - descriptive name
-   **`always_run`**: Optional - run stage even if previous stages failed (defaults to false)
-   **`request`**: Required - HTTP request configuration
-   **`response`**: Optional - response handling configuration

**Request fields:**

-   **`url`**: Required - endpoint URL
-   **`method`**: Optional - HTTP method (defaults to GET)
-   **`params`**: Optional - query parameters
-   **`headers`**: Optional - HTTP headers
-   **`body`**: Optional - request body (see Body Types section below)
-   **`timeout`**: Optional - request timeout in seconds (float)

### Request Body Types

The `body` field supports different content types. Only one body type can be specified per request:

#### JSON Body

```json
{
    "request": {
        "body": {
            "json": {
                "name": "John Doe",
                "age": 30,
                "active": true
            }
        }
    }
}
```

#### Form Data (application/x-www-form-urlencoded)

```json
{
    "request": {
        "body": {
            "form": {
                "username": "johndoe",
                "password": "secret123",
                "remember": "true"
            }
        }
    }
}
```

#### XML Body

```json
{
    "request": {
        "headers": {
            "Content-Type": "application/xml"
        },
        "body": {
            "xml": "<user><name>John Doe</name><age>30</age></user>"
        }
    }
}
```

#### Raw Text Body

```json
{
    "request": {
        "headers": {
            "Content-Type": "text/plain; charset=utf-8"
        },
        "body": {
            "raw": "This is raw text content"
        }
    }
}
```

#### File Upload (multipart/form-data)

```json
{
    "request": {
        "body": {
            "files": {
                "document": "path/to/file.pdf",
                "config": "/etc/important.conf"
            }
        }
    }
}
```

**File Upload Notes:**

-   Use file paths directly (e.g., `/path/to/file` or `relative/path/file.txt`)
-   Files are automatically opened in binary mode
-   Relative and absolute paths are supported

**Response fields:**

-   **`response`**: Optional - response handling configuration
-   **`save.vars`**: JMESPath expressions to extract data
-   **`save.functions`**: User functions to extract data
-   **`verify.status`**: Expected HTTP status code
-   **`verify.headers`**: Expected response headers (case-insensitive)
-   **`verify.vars`**: direct assertions for variables
-   **`verify.functions`**: User functions for non-trivial assertions
-   **`verify.body`**: Response body validation (schema, substring matching, and/or regex patterns)
    -   **`schema`**: JSON schema for validating response structure
    -   **`contains`**: List of substrings that must be present in the response body
    -   **`not_contains`**: List of substrings that must NOT be present in the response body
    -   **`matches`**: List of regex patterns that must match the response body
    -   **`not_matches`**: List of regex patterns that must NOT match the response body

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
                    "kwargs": { "threshold": 5 }
                }
            ]
        }
    }
}
```

### Request Timeout

You can specify a timeout for individual requests to prevent hanging on slow or unresponsive servers:

```json
{
    "stages": [
        {
            "name": "quick_request",
            "request": {
                "url": "https://api.example.com/endpoint",
                "method": "GET",
                "timeout": 5.0
            },
            "response": {
                "verify": {
                    "status": 200
                }
            }
        },
        {
            "name": "slow_operation",
            "request": {
                "url": "https://api.example.com/long-running-task",
                "method": "POST",
                "timeout": 60.0,
                "body": {
                    "json": { "task": "process_data" }
                }
            }
        }
    ]
}
```

If the request exceeds the specified timeout (in seconds), the test will fail with a timeout error.

### Cleanup and Always-Run Stages

By default, if any stage in a scenario fails, subsequent stages are skipped. However, you can mark stages with `always_run: true` to ensure they execute regardless of previous failures. This is particularly useful for cleanup operations:

```json
{
    "stages": [
        {
            "name": "create_resource",
            "request": {
                "url": "https://api.example.com/resources",
                "method": "POST",
                "body": {
                    "json": { "name": "test-resource" }
                }
            },
            "response": {
                "save": {
                    "vars": { "resource_id": "id" }
                }
            }
        },
        {
            "name": "test_resource",
            "request": {
                "url": "https://api.example.com/resources/{{ resource_id }}/test",
                "method": "POST"
            },
            "response": {
                "verify": { "status": 200 }
            }
        },
        {
            "name": "cleanup_resource",
            "always_run": true,
            "request": {
                "url": "https://api.example.com/resources/{{ resource_id }}",
                "method": "DELETE"
            }
        }
    ]
}
```

In this example:
- If `create_resource` fails, both `test_resource` and `cleanup_resource` are skipped
- If `test_resource` fails, `cleanup_resource` still runs due to `always_run: true`
- This ensures the test resource is cleaned up even if the test fails

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
    "stages": [
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
    "stages": [
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

-   `AWS_PROFILE` → `profile`
-   `AWS_ACCESS_KEY_ID` → `access_key_id`
-   `AWS_SECRET_ACCESS_KEY` → `secret_access_key`
-   `AWS_SESSION_TOKEN` → `session_token`
-   `AWS_DEFAULT_REGION` → `region`

This allows you to omit credentials from JSON files:

```json
{
    "aws": {
        "service": "execute-api"
    },
    "stages": [...]
}
```

#### Required Fields

-   **`service`**: AWS service name (e.g., `execute-api`, `s3`, `lambda`)

### Variable Substitution

The plugin uses **Jinja2 templates** for powerful variable substitution, supporting complex data access patterns. Note that every string is treated as independent Jinja2 environment. This means, while you can use Jinja2 syntax within a string, you can't make the whole JSON file a Jinja2 template, it'll break validation.

#### Initial Variables

You can define initial variables at the scenario level using the `vars` field. These variables are available throughout all stages:

```json
{
    "vars": {
        "base_url": "https://api.example.com",
        "api_version": "v2",
        "default_timeout": 30
    },
    "stages": [
        {
            "name": "get_users",
            "request": {
                "url": "{{ base_url }}/{{ api_version }}/users",
                "timeout": "{{ default_timeout }}"
            }
        }
    ]
}
```

Initial variables are useful for:

-   Defining common values used across multiple stages (base URLs, API keys, etc.)
-   Setting default configuration values that can be overridden
-   Parameterizing tests without using fixtures

**Variable Overwriting Rules**:

-   Initial variables can be overwritten by saved variables in later stages
-   Fixtures cannot be overwritten and cannot shadow initial variables
-   Saved variables cannot conflict with fixture names

#### Basic Variable Access

```json
{
    "request": {
        "url": "https://api.example.com/users/{{ user_id }}",
        "headers": { "Authorization": "Bearer {{ auth_token }}" }
    }
}
```

#### Object Dot Notation

Access nested object properties using dot notation:

```json
{
    "request": {
        "url": "https://api.example.com/users/{{ user.profile.id }}",
        "headers": { "X-User-Role": "{{ user.permissions.role }}" }
    }
}
```

#### Array/List Access

Access array elements using square brackets:

```json
{
    "request": {
        "url": "https://api.example.com/items/{{ items[0] }}/details",
        "body": {
            "json": { "categories": "{{ categories[1] }}" }
        }
    }
}
```

#### Complex Nested Access

Combine dot notation and array access for complex data structures:

```json
{
    "request": {
        "url": "https://api.example.com/users/{{ data.users[0].profile.id }}",
        "body": {
            "json": {
                "primary_address": "{{ user.addresses[0].street }}",
                "backup_email": "{{ user.contacts.emails[1] }}"
            }
        }
    }
}
```

### Using saved data

#### Shaping further stages

Use saved data from previous stages to alter URLs, query parameters, headers etc.

```json
{
    "stages": [
        {
            "name": "login",
            "request": { "url": "/auth" },
            "response": {
                "save": { "vars": { "token": "access_token" } }
            }
        },
        {
            "name": "api_call",
            "request": {
                "url": "/api/data",
                "headers": { "Authorization": "Bearer {{ token }}" }
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
                    "kwargs": { "threshold": 5 }
                }
            ]
        }
    }
}
```

### Response Body Verification

#### JSON Schema Validation

Validate response body structure against a JSON schema:

```json
{
    "response": {
        "verify": {
            "body": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "id": { "type": "integer" },
                        "name": { "type": "string" },
                        "active": { "type": "boolean" }
                    },
                    "required": ["id", "name"]
                }
            }
        }
    }
}
```

You can also reference schema from a file:

```json
{
    "response": {
        "verify": {
            "body": {
                "schema": "schemas/user_response.json"
            }
        }
    }
}
```

#### Substring Matching

For simple text verification, use substring matching without regex complexity:

```json
{
    "response": {
        "verify": {
            "body": {
                "contains": ["Success", "User created", "user@example.com"],
                "not_contains": ["error", "failed", "unauthorized"]
            }
        }
    }
}
```

**Substring Matching Notes:**

-   **`contains`**: All substrings in this list must be found in the response body
-   **`not_contains`**: None of the substrings in this list should be found in the response body
-   Case-sensitive exact substring matching (no pattern interpretation)
-   Useful for quick content checks without regex complexity

#### Regex Pattern Matching

For more complex pattern matching, use regular expressions:

```json
{
    "response": {
        "verify": {
            "body": {
                "matches": ["User ID: \\d+", "email@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}", "<title>.*Success.*</title>"],
                "not_matches": ["error", "failed", "<script>"]
            }
        }
    }
}
```

**Pattern Matching Notes:**

-   **`matches`**: All patterns in this list must match the response body
-   **`not_matches`**: None of the patterns in this list should match the response body
-   Patterns are applied to the raw response text (not just JSON)
-   Standard Python regex syntax is supported
-   Useful for HTML responses, error detection, or content validation

#### Combining All Validation Methods

You can use schema validation, substring matching, and regex patterns together:

```json
{
    "response": {
        "verify": {
            "status": 200,
            "body": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["active", "inactive"]},
                        "data": {"type": "object"}
                    }
                },
                "contains": [
                    "active",
                    "data"
                ],
                "not_contains": [
                    "error",
                    "deprecated"
                ],
                "matches": [
                    "\"id\":\\s*\\d+",
                    "\"timestamp\":\\s*\"\\d{4}-\\d{2}-\\d{2}\""
                ],
                "not_matches": [
                    "password",
                    "secret"
                ]
            }
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
        "request": { "url": "/auth/login" },
        "response": { "save": { "vars": { "token": "access_token" } } }
    }
}
```

**test_api.http.json:**

```json
{
    "stages": [
        { "$ref": "stage_common.json#/authenticate" },
        {
            "name": "get_data",
            "request": {
                "url": "/data",
                "headers": { "Authorization": "Bearer {{ token }}" }
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
    "stages": [
        {
            "name": "test_with_fixtures",
            "request": { "url": "http://{{ server }}/api/{{ auth_token }}" }
        }
    ]
}
```

#### Marks

Apply pytest marks:

```json
{
    "marks": ["skip(reason='API not ready')", "xfail"]
}
```

Note: The following markers are **not supported**: `skipif`, `usefixture`, and `parametrize`.

## File Naming Convention

Test files must follow the pattern: `test_<name>.<suffix>.json`

-   **Prefix**: Must start with `test_`
-   **Name**: Descriptive test name
-   **Suffix**: Configurable (default: `http`)
-   **Extension**: Must be `.json`

Examples:

-   `test_user_api.http.json`
-   `test_authentication.http.json`
-   `test_payment_flow.http.json`

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

-   **Package manager**: `uv`
-   **Linting/formatting**: `ruff`
-   **Testing**: `pytest`

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

-   **pytest**: Test framework integration
-   **jsonref**: JSON reference resolution
-   **pydantic**: Data validation and parsing
-   **jmespath**: JSON query language for response data extraction
-   **jinja2**: Template engine for variable substitution
-   **requests**: HTTP client

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

-   **JSON validation errors**: Invalid test file syntax
-   **Pydantic validation errors**: Invalid test file structure
-   **Variable substitution errors**: Missing or invalid variables
-   **HTTP request errors**: Network or server issues
-   **JMESPath errors**: Invalid query expressions
-   **Function import errors**: Missing or invalid user functions

## Best Practices

-   **Descriptive stage names**: Use clear, action-oriented names
-   **Modular design**: Use `$ref` for reusable components
-   **Cleanup**: Use `always_run: true` stages for cleanup operations
-   **Fixtures**: Leverage pytest fixtures for test data and setup

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
            "args": ["run", "pytest-http-mcp"],
            "env": {}
        }
    }
}
```

### Features

The MCP server provides:

-   **JSON Validation**: Verify HTTP test JSON files against the Scenario schema
-   **Schema Export**: Access the complete JSON schema for test file structure
-   **Example Generation**: Get complete example test scenarios

## Examples

See the `tests/integration/examples/` directory for complete working examples of:

-   Basic HTTP requests and responses
-   Multi-stage scenarios with variable passing
-   pytest mark usage
-   JSON reference usage
-   Custom function integration
