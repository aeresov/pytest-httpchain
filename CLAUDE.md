# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

pytest-httpchain is a pytest plugin for declarative HTTP API integration testing. Test scenarios are defined in JSON files with `$ref` support, template expressions (`{{ expr }}`), and multi-stage request chaining.

## Commands

```bash
# Run all tests
uv run pytest

# Run a single test file or test
uv run pytest tests/integration/test_primer.py -v
uv run pytest tests/unit/test_foo.py::test_specific -v

# Lint — CI's Lint job runs ALL FOUR of these; run them all before pushing
uv run ruff check .
uv run ruff format --check .
uvx ty@0.0.49 check     # type check (pin matches CI)
uv run lint-imports     # import layering contracts (exhaustive: new modules fail until placed)

# Format
uv run ruff format .

# Validate scenario file(s) (exits non-zero if invalid)
uv run pytest-httpchain validate tests/integration/examples/save/test_save_jmespath.http.json

# Deep validation (opt-in): import user functions + check signatures + referenced files
uv run pytest-httpchain validate --deep --syspath tests/integration/examples tests/integration/examples/save/test_save_user_function.http.json

# Run unit tests with coverage report
uv run pytest tests/unit --cov=src --cov-report=term-missing
```

## Architecture

The plugin is a single distribution; domain subpackages (models, templates, jsonref, userfunc) live under `src/pytest_httpchain/` and must not import plugin modules (enforced by import-linter).

```
src/pytest_httpchain/
├── cli.py                     # Typer CLI (validate, schema, resolve, show, graph)
├── validation.py              # Shared validator (CLI + collection-time): coded Diagnostic objects (HTTPCHAINxxx) for semantic checks incl. order-aware data-flow; plus opt-in `check_scenario_deep` (imports/signatures/files) used only by `validate --deep`
├── dataflow.py                # DataFlow model + analyze_dataflow() (stage data-flow analysis, used by show/graph)
├── scoping.py                 # Single encoding of the scope/visibility rules: StageScopes static name sets (used by validation + dataflow) and runtime ChainMap context builders (used by carrier)
├── schema.py                  # build_schema() — JSON Schema generation shared by the schema command
├── plugin.py                  # pytest hooks, JSON test file collection (JsonModule), chain-contiguity ordering hooks
├── factory.py                 # Collection-time test-class factory (create_test_class)
├── carrier.py                 # Runtime execution engine (Carrier class)
├── utils.py                   # Marker construction, substitution processing, small shared helpers
├── report_formatter.py        # HTTP request/response formatting for test reports
├── har_writer.py              # HAR file export for HTTP request/response logging
├── constants.py               # ConfigOptions enum for pytest.ini settings + the shared user-function name grammar
├── errors.py                  # HttpChainError (base) + StageExecutionError (carries request/response) + subclasses RequestError, SaveError, VerificationError
├── userfunc.py                # Dynamic function import/invocation, incl. the model-aware call_user_function dispatch
├── models/                    # Pydantic models (Scenario, Stage, Request, etc.)
├── templates/                 # {{ expression }} substitution engine
└── jsonref/                   # $ref resolution with deep merging
```

The models, templates, and jsonref subpackages carry their own CLAUDE.md next to their code.

## Test File Pattern

Test scenarios are discovered by pattern: `test_<name>.http.json` (suffix configurable via `httpchain_suffix` ini option).

## Key Execution Flow

1. **Collection**: `plugin.py:JsonModule.collect()` loads JSON, resolves `$ref`, validates against `Scenario` model, then runs `validation.py:check_scenario()` which returns coded `Diagnostic` objects — error-severity → `CollectError`, warning-severity → `ScenarioValidationWarning`
2. **Class generation**: `factory.py:create_test_class()` creates dynamic test class with stage methods
3. **Execution**: Each stage method calls `Carrier.execute_stage()` which:
   - Processes substitutions into context
   - Walks request model through template engine
   - Executes HTTP request via httpx
   - Processes response steps (verify/save)
   - Updates global context with saved values

## Integration Tests

Integration tests use pytest's `pytester` fixture. Test scenarios live in `tests/integration/examples/` and are executed via pytester's `runpytest()`.
