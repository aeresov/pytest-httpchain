# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`pytest-httpchain` is a pytest plugin for integration testing of HTTP APIs. It uses declarative JSON files to define multi-stage test scenarios with variable substitution, response verification, and data extraction capabilities.

## Architecture

The project uses a monorepo structure with the main plugin and several sub-packages:

- **Main Plugin** (`src/pytest_httpchain/`): Core plugin that discovers and executes HTTP test scenarios from JSON files
- **Sub-packages** (`packages/`):
  - `pytest-httpchain-jsonref`: Handles JSON references ($ref) for test composition
  - `pytest-httpchain-models`: Pydantic models for test scenario validation
  - `pytest-httpchain-templates`: Jinja2 template processing for variable substitution
  - `pytest-httpchain-userfunc`: User-defined Python function integration
  - `pytest-httpchain-mcp`: MCP server for AI assistant integration

Key components:
- `plugin.py`: Pytest hooks for test discovery and collection
- `carrier.py`/`carrier_factory.py`: Dynamic test class generation from JSON scenarios
- `stage_executor.py`: Executes individual HTTP request stages
- `context.py`: Manages shared data context across test stages

## Development Commands

```bash
# Install dependencies and sync all packages
uv sync --all-extras --all-packages

# Format and lint code
make tidyup
# Or directly:
uv run ruff check --fix --unsafe-fixes .
uv run ruff format .

# Run tests
uv run pytest tests/

# Run specific test file or pattern
uv run pytest tests/integration/test_dummy.py
uv run pytest -k "test_name_pattern"

# Run tests with verbose output
uv run pytest -v

# Run a single test stage
uv run pytest -k "test_0_one" tests/integration/examples/dummy/
```

## Test File Format

HTTP chain tests are JSON files named `test_*.http.json` (configurable suffix). Example structure:

```json
{
    "vars": {"key": "value"},
    "fixtures": ["fixture_name"],
    "marks": ["pytest_marker"],
    "stages": [
        {
            "name": "stage_name",
            "request": {
                "url": "{{ variable_substitution }}",
                "method": "GET"
            },
            "response": [
                {
                    "verify": {"status": 200}
                },
                {
                    "save": {
                        "vars": {"saved_var": "jmespath.expression"}
                    }
                }
            ]
        }
    ]
}
```

## Variable Substitution

Uses Jinja2-style expressions `{{ variable }}` in JSON values. Variables come from:
- Test-level `vars` declarations
- Pytest fixtures
- Data saved from previous stages
- Python expressions can be used inline: `{{ str(number_value) }}`

## Testing Patterns

- Test scenarios act as pytest test classes
- Stages act as individual test methods
- Stages execute sequentially, sharing data context
- Use `always_run: true` for cleanup stages
- Reference external JSON files with `$ref` for reusability