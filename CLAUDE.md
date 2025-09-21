# Project Overview

`pytest-httpchain` is a pytest plugin for declarative HTTP API integration testing. Tests are defined in JSON files that describe multi-stage HTTP request/response scenarios with variable substitution, response verification, and data extraction capabilities.

# Architecture

## Core Flow

1. **Test Discovery**: `plugin.py` implements pytest hooks to find `test_*.http.json` files and creates `JsonModule` instances
2. **Test Generation**: `carrier.py` dynamically creates test classes from JSON scenarios
3. **Test Execution**: Each stage becomes a test method that:
    - Prepares context with variables and fixtures
    - Executes HTTP request
    - Verifies response and saves data for next stages
    - Updates global context for subsequent stages

## Key Components

**Main Plugin** (`src/pytest_httpchain/`)

-   `plugin.py`: Pytest hooks for test discovery and collection
-   `carrier.py`: Dynamic test class generation, manages test lifecycle and shared state
-   `context_manager.py`: Manages layered data context
-   `request.py`: HTTP request preparation and execution
-   `response.py`: Response processing, verification, and data extraction
-   `fixture_manager.py`: Handles pytest fixture processing for tests

**Sub-packages** (`packages/`)

-   `pytest-httpchain-models`: Pydantic models for JSON schema validation
-   `pytest-httpchain-templates`: Jinja2 template substitution engine
-   `pytest-httpchain-jsonref`: JSON reference resolution ($ref support)
-   `pytest-httpchain-userfunc`: User function wrapping and execution
-   `pytest-httpchain-mcp`: MCP server for AI assistant integration

## Key Concepts

### Variable Substitution

-   Jinja2-style expressions `{{ variable }}` in JSON values
-   Python expressions supported: `{{ str(value) }}`, `{{ len(items) }}`
-   Variables resolved from context

### Stage Execution

-   Stages execute sequentially with shared context
-   Failed stages abort flow unless marked with `xfail`
-   `always_run: true` ensures cleanup stages run regardless of failures

### Response Processing

-   **Save**: Extract data using JMESPath or custom functions
-   **Verify**: Assert status codes, headers, JSON schema, expressions
-   Saved variables available to all subsequent stages

### JSONRef Support

-   Reuse test components with `$ref` references
-   Supports local and cross-file references
-   Properties merged recursively with type checking
