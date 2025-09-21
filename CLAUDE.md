# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`pytest-httpchain` is a pytest plugin for declarative HTTP API integration testing. Tests are defined in JSON files that describe multi-stage HTTP request/response scenarios with variable substitution, response verification, and data extraction capabilities.

## Architecture

### Core Flow
1. **Test Discovery**: `plugin.py` finds `test_*.http.json` files and creates `JsonModule` instances
2. **Test Generation**: `carrier.py` dynamically creates test classes from JSON scenarios
3. **Test Execution**: Each stage becomes a test method that:
   - Prepares context with variables and fixtures
   - Executes HTTP request with template substitution
   - Verifies response and saves data for next stages
   - Updates global context for subsequent stages

### Key Components

**Main Plugin** (`src/pytest_httpchain/`)
- `plugin.py`: Pytest hooks for test discovery and collection
- `carrier.py`: Dynamic test class generation, manages test lifecycle and shared state
- `context_manager.py`: Manages variable context layers (stage > seed > fixtures > global)
- `request.py`: HTTP request preparation and execution
- `response.py`: Response processing, verification, and data extraction
- `fixture_manager.py`: Handles pytest fixture processing for tests

**Sub-packages** (`packages/`)
- `pytest-httpchain-models`: Pydantic models for JSON schema validation
- `pytest-httpchain-templates`: Jinja2 template substitution engine
- `pytest-httpchain-jsonref`: JSON reference resolution ($ref support)
- `pytest-httpchain-userfunc`: User function wrapping and execution
- `pytest-httpchain-mcp`: MCP server for AI assistant integration

### Context Hierarchy
Variables are resolved with this precedence (highest to lowest):
1. Stage-level variables (`stage.substitutions`)
2. Scenario-level seed context (functions and vars from `scenario.substitutions`)
3. Pytest fixtures (processed and cached)
4. Global context (data saved from previous stages)

## Development Commands

```bash
# Install dependencies and sync all packages
uv sync --all-extras --all-packages

# Format and lint code
make tidyup
# Or directly:
uv run ruff check --fix --unsafe-fixes .
uv run ruff format .

# Run all tests
uv run pytest tests/

# Run specific test scenarios
uv run pytest tests/integration/examples/dummy/test_blip.http.json
uv run pytest tests/integration/examples/dummy/  # all scenarios in directory

# Run specific stage within a scenario
uv run pytest test_blip.http.json::blip::test_0_one -v

# Run with verbose output and logging
uv run pytest -v -s --log-cli-level=INFO

# Run tests matching pattern
uv run pytest -k "test_name_pattern"
```

## Test File Format

HTTP chain tests are JSON files named `test_*.http.json` (suffix configurable via `pytest.ini`):

```json
{
    "substitutions": [
        {
            "functions": {
                "func_alias": {"name": "module:function"}
            },
            "vars": {"base_url": "http://api.example.com"}
        }
    ],
    "fixtures": ["session_fixture"],
    "marks": ["slow"],
    "auth": {"name": "auth:basic", "kwargs": {"username": "user", "password": "pass"}},
    "stages": [
        {
            "name": "login",
            "fixtures": ["user_data"],
            "parameters": [
                {"individual": {"status": [200, 201]}}
            ],
            "request": {
                "url": "{{ base_url }}/login",
                "method": "POST",
                "body": {"json": {"username": "{{ username }}"}}
            },
            "response": [
                {"verify": {"status": "{{ status }}"}},
                {"save": {"vars": {"token": "auth.token"}}}
            ]
        }
    ]
}
```

## Key Concepts

### Variable Substitution
- Jinja2 expressions `{{ variable }}` in JSON values
- Python expressions supported: `{{ str(value) }}`, `{{ len(items) }}`
- Variables resolved from context hierarchy at runtime

### Stage Execution
- Stages execute sequentially with shared context
- Failed stages abort flow unless marked with `xfail`
- `always_run: true` ensures cleanup stages run regardless of failures
- Each stage can have parameters for data-driven testing

### Response Processing
- **Save**: Extract data using JMESPath or custom functions
- **Verify**: Assert status codes, headers, JSON schema, expressions
- Saved variables available to all subsequent stages

### JSONRef Support
- Reuse test components with `$ref` references
- Supports local and cross-file references
- Properties merged recursively with type checking