# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Package Management and Dependencies
- **Install dependencies**: `uv sync --all-extras --all-packages`
- **Update dependencies**: `uv lock --upgrade`

### Testing
- **Run all tests**: `uv run pytest`
- **Run specific test file**: `uv run pytest tests/unit/test_models.py`
- **Run integration tests**: `uv run pytest tests/integration/`
- files in `tests/integration/examples/` are used in integration tests and are not individual test targets

### Code Quality
- **Lint code**: `uv run ruff check`
- **Format code**: `uv run ruff format`  
- **Auto-fix and format**: `make tidyup` (runs both ruff check --fix --unsafe-fixes and ruff format)

## Architecture Overview

This is a pytest plugin that enables HTTP testing through JSON configuration files. The project is organized as a monorepo with multiple packages:

### Core Components

1. **pytest-http** (main package): Contains the pytest plugin implementation
   - `src/pytest_http/plugin.py`: Main pytest plugin with collection and execution logic
   - Entry point: `pytest_http.pytest_plugin` (configured in pyproject.toml)

2. **engine** (workspace package): Core HTTP testing engine
   - `packages/engine/src/engine/models.py`: Pydantic models for JSON schema validation
   - `packages/engine/src/engine/types.py`: Type definitions and validators
   - `packages/engine/src/engine/user_function.py`: User function handling utilities

3. **mcp-server** (workspace package): MCP server implementation
   - `packages/mcp-server/src/mcp_server/`: MCP server components

### Key Architecture Details

- **Plugin System**: Uses pytest's plugin architecture with file collection hooks
- **JSON Schema Validation**: Pydantic models validate JSON test files against strict schemas
- **Variable Substitution**: Template engine for passing data between HTTP request stages
- **AWS Authentication**: Optional AWS SigV4 authentication support
- **User Functions**: Extensible system for custom validation and data extraction logic

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

## Configuration

The plugin supports configuration through:
- `pytest.ini`: `[pytest]` section with `suffix = custom_suffix`
- `pyproject.toml`: `[tool.pytest.ini_options]` with `suffix = custom_suffix`

## Dependencies

- **pytest**: Core testing framework
- **pydantic**: Data validation and parsing
- **requests**: HTTP client
- **jmespath**: JSON query language for data extraction  
- **jsonref**: JSON reference resolution ($ref support)
- **ruff**: Linting and formatting
- **uv**: Package management

Optional dependencies:
- **aws**: `requests-auth-aws-sigv4`, `boto3` for AWS authentication
- **mcp**: `mcp-server` for MCP functionality

## Current State

The codebase is in development with recent restructuring. The main pytest plugin entry point is configured but there may be import path issues that need resolution during development.
