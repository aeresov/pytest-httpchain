# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

pytest-httpchain is a pytest plugin for declarative HTTP API integration testing. Test scenarios are defined in JSON files with `$ref` support, template expressions (`{{ expr }}`), and multi-stage request chaining.

## Commands

```bash
# Run all tests
uv run pytest

# Run tests for a specific package
uv run pytest packages/pytest-httpchain-jsonref/tests -v
uv run pytest packages/pytest-httpchain-models/tests -v
uv run pytest packages/pytest-httpchain-templates/tests -v
uv run pytest packages/pytest-httpchain-mcp/tests -v
uv run pytest packages/pytest-httpchain-userfunc/tests -v

# Run a single test file or test
uv run pytest tests/integration/test_primer.py -v
uv run pytest tests/unit/test_foo.py::test_specific -v

# Lint
uv run ruff check .
uv run ruff format --check .

# Format
uv run ruff format .

# Run MCP server
uv run pytest-httpchain-mcp

# Run unit tests with coverage report (main package only)
uv run pytest tests/unit --cov=src --cov-report=term-missing

# Run all tests (may have conftest conflicts, use specific paths)
uv run pytest tests/unit tests/integration -v
```

## Architecture

This is a uv workspace monorepo with the main plugin in `src/` and supporting packages in `packages/`:

```
src/pytest_httpchain/          # Main pytest plugin
├── plugin.py                  # pytest hooks, JSON test file collection (JsonModule)
├── carrier.py                 # Test execution engine (Carrier class)
├── utils.py                   # Substitution processing, user function calls
└── report_formatter.py        # HTTP request/response formatting for test reports

packages/
├── pytest-httpchain-jsonref/       # $ref resolution with deep merging
├── pytest-httpchain-models/        # Pydantic models (Scenario, Stage, Request, etc.)
├── pytest-httpchain-templates/     # {{ expression }} substitution engine
├── pytest-httpchain-userfunc/      # Dynamic function import/invocation
└── pytest-httpchain-mcp/           # MCP server for AI assistants
```

Each package has its own CLAUDE.md with detailed API and behavior documentation.

## Test File Pattern

Test scenarios are discovered by pattern: `test_<name>.http.json` (suffix configurable via `suffix` ini option).

## Key Execution Flow

1. **Collection**: `plugin.py:JsonModule.collect()` loads JSON, resolves `$ref`, validates against `Scenario` model
2. **Class generation**: `carrier.py:create_test_class()` creates dynamic test class with stage methods
3. **Execution**: Each stage method calls `Carrier.execute_stage()` which:
   - Processes substitutions into context
   - Walks request model through template engine
   - Executes HTTP request via httpx
   - Processes response steps (verify/save)
   - Updates global context with saved values

## Integration Tests

Integration tests use pytest's `pytester` fixture. Test scenarios live in `tests/integration/examples/` and are executed via pytester's `runpytest()`.
