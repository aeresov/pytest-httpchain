# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Package Management and Dependencies
- **Python requirement**: Python >=3.13
- **Install dependencies**: `uv sync --all-extras --all-packages`
- **Install dev dependencies**: `make sync` (runs uv sync --dev --all-extras --all-packages)
- **Update dependencies**: `uv lock --upgrade`
- **Build packages**: `uv build`

### Testing
- **Run all tests**: `uv run pytest`
- **Run tests with verbose output**: `uv run pytest -v`
- **Run tests with print statements**: `uv run pytest -s`
- **Run specific test file**: `uv run pytest tests/unit/test_aws_auth.py`
- **Run integration tests**: `uv run pytest tests/integration/`
- **Run specific JSON test**: `uv run pytest tests/integration/examples/test_full.http.json`
- **Note**: Files in `tests/integration/examples/` are used in integration tests and are not individual test targets

### Code Quality
- **Lint code**: `uv run ruff check`
- **Format code**: `uv run ruff format`  
- **Auto-fix and format**: `make tidyup` (runs both ruff check --fix --unsafe-fixes and ruff format)

## Architecture Overview

This is a pytest plugin that enables HTTP testing through JSON configuration files. The project is organized as a monorepo with multiple packages:

### Core Components

1. **pytest-http** (main package): Contains the pytest plugin implementation
   - `src/pytest_http/plugin.py`: Main pytest plugin with collection and execution logic
   - Entry point: `pytest_http.plugin` (configured in pyproject.toml)

2. **pytest-http-engine** (workspace package): Core HTTP testing engine
   - `packages/pytest-http-engine/src/pytest_http_engine/models.py`: Pydantic models for JSON schema validation
   - `packages/pytest-http-engine/src/pytest_http_engine/types.py`: Type definitions and validators
   - `packages/pytest-http-engine/src/pytest_http_engine/user_function.py`: User function handling utilities

3. **pytest-http-mcp** (workspace package): MCP server implementation
   - `packages/pytest-http-mcp/src/pytest_http_mcp/`: MCP server components

### Key Architecture Details

- **Plugin System**: Uses pytest's plugin architecture with file collection hooks in `src/pytest_http/plugin.py:434`
- **JSON Schema Validation**: Pydantic models validate JSON test files against strict schemas
- **Variable Substitution**: Jinja2 template engine for passing data between HTTP request stages (`substitute_variables` function)
- **AWS Authentication**: Optional AWS SigV4 authentication support via `create_aws_auth` function
- **User Functions**: Extensible system for custom validation and data extraction logic
- **Stage Execution**: Sequential execution of flow stages, with final stages always executed for cleanup
- **Session Management**: HTTP session per scenario with proper setup/teardown lifecycle
- **Error Handling**: HTTP errors and validation failures produce descriptive pytest failure messages
- **Mock Server**: Integration tests use `http-server-mock` for reliable testing

### File Naming Convention

JSON test files must follow: `test_<name>.<suffix>.json` where suffix defaults to "http" but can be configured via pytest.ini or pyproject.toml.

### Test File Structure

JSON test files contain:
- `fixtures`: pytest fixture names to inject
- `marks`: pytest marks to apply
- `aws`: AWS authentication configuration (optional)
- `flow`: Main test stages (executed in order)
- `final`: Cleanup stages (always executed)

Each stage defines:
- `request`: HTTP request details (URL, method, headers, body)
- `response`: Response handling (save variables, verify status/data)

### Variable Substitution

The plugin uses Jinja2 templates for powerful variable substitution that allows passing data between stages:

- **Format**: `{{ variable_name }}` (double curly braces)
- **Features**: Supports object dot notation, array access, and Jinja2 filters
- **Usage**: Variables saved in earlier stages can be referenced in later stages
- **Context**: All saved variables are available in the variable context for subsequent stages
- **Examples**:
  - Simple variables: `"https://api.example.com/users/{{ user_id }}/profile"`
  - Object properties: `"Authorization": "Bearer {{ auth.token }}"`
  - Array access: `"https://api.example.com/items/{{ items[0] }}/details"`
  - Nested access: `"https://api.example.com/users/{{ data.users[0].profile.id }}"`
  - JSON body: `"user_id": "{{ user.id }}", "name": "{{ user.name }}"`

Variables are saved using JMESPath expressions in the `response.save.vars` section:
```json
"response": {
  "save": {
    "vars": {
      "auth_token": "data.token",
      "user_id": "data.user.id"
    }
  }
}
```

## Configuration

The plugin supports configuration through:
- `pytest.ini`: `[pytest]` section with `suffix = custom_suffix`
- `pyproject.toml`: `[tool.pytest.ini_options]` with `suffix = custom_suffix`

## Dependencies

Core dependencies:
- **pytest**: Core testing framework
- **pydantic**: Data validation and parsing using `pytest_http_engine.models`
- **requests**: HTTP client for making API calls
- **jmespath**: JSON query language for response data extraction  
- **jsonref**: JSON reference resolution ($ref support) for reusable stage definitions
- **jinja2**: Template engine for variable substitution
- **ruff**: Linting and formatting
- **uv**: Package management

Optional dependencies:
- **aws**: `requests-auth-aws-sigv4`, `boto3` for AWS SigV4 authentication
- **mcp**: `pytest-http-mcp` package for MCP server functionality

Development dependencies:
- **http-server-mock**: Mock HTTP server for testing
- **responses**: HTTP response mocking library

## Plugin Entry Point

The pytest plugin is configured in `pyproject.toml` with entry point `pytest_http = "pytest_http.plugin"` which makes it automatically discoverable by pytest when the package is installed.
