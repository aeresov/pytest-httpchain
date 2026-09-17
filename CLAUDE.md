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

# Faster full run. Almost all the wall time is integration tests that each spawn
# a pytester session and its own HTTP server, and they are independent, so the
# suite parallelizes cleanly (~4 min -> ~2 min on 4 cores). CI passes `-n auto`.
uv run pytest -n auto

# Faster feedback loop while iterating. The unit suite alone is ~1000 tests in
# under 10 seconds; `-m "not slow"` then drops the subprocess-spawning and
# sleep-bound integration families (~27 tests, ~40% of integration wall time).
# Both are for iterating only — run the whole suite before pushing.
uv run pytest tests/unit
uv run pytest -m "not slow"

# Lint — CI's Lint job runs ALL FIVE of these; run them all before pushing
uv run ruff check .
uv run ruff format --check .
uvx ty@0.0.49 check     # type check (pin matches CI)
uv run lint-imports     # import layering contracts (exhaustive: new modules fail until placed)
# Regenerate the committed JSON Schema after ANY pydantic model change. The
# generator always exits 0, so the `git diff` is the actual gate — without it a
# model change passes locally and then reddens CI on a generated file you never
# edited. Commit the regenerated files.
uv run python scripts/generate_schema.py && git add -N docs/schema && git diff --exit-code docs/schema

# Format
uv run ruff format .

# Validate scenario file(s) (exits non-zero if invalid)
uv run pytest-httpchain validate tests/integration/examples/save/test_save_jmespath.http.json

# Deep validation (opt-in): import user functions + check signatures + referenced files
uv run pytest-httpchain validate --deep --syspath tests/integration/examples tests/integration/examples/save/test_save_user_function.http.json

# Run tests with coverage report.
# Use `coverage run -m pytest` (NOT `pytest --cov`, which is not installed): the
# plugin is imported via its pytest11 entry point before a pytest plugin could start
# recording, so `--cov` would count every module-level line (model fields, class
# bodies) as missed and report a ~20-point-low floor. CI uses this form.
# `parallel` + `patch=subprocess` measure pytester subprocesses too and write
# pid-suffixed data files — always `combine` before `report`.
# Run the WHOLE suite: `fail_under = 94` applies to every `coverage report`, and
# unit tests alone reach ~87, so `tests/unit` here would always exit non-zero.
uv run coverage run -m pytest tests
uv run coverage combine
uv run coverage report --show-missing

# Unit-only variant (faster, but below the project floor — opt out of the gate)
uv run coverage run -m pytest tests/unit
uv run coverage combine
uv run coverage report --show-missing --fail-under=0
```

## Architecture

The plugin is a single distribution; domain subpackages (models, templates, jsonref, userfunc) live under `src/pytest_httpchain/` and must not import plugin modules (enforced by import-linter).

```
src/pytest_httpchain/
├── cli.py                     # Typer CLI (validate, schema, resolve, show, graph)
├── validation/                # Shared validator (CLI + collection-time), a package: diagnostics (codes/result types), loader ($ref + model validation), semantic (checks incl. order-aware data-flow), deep (imports/signatures/files, `validate --deep` only), validate (file-level entry point)
├── dataflow.py                # DataFlow model + analyze_dataflow() (stage data-flow analysis, used by show/graph)
├── scoping.py                 # Single encoding of the scope/visibility rules: StageScopes static name sets (used by validation + dataflow) and runtime ChainMap context builders (used by carrier)
├── schema.py                  # build_schema() — JSON Schema generation shared by the schema command
├── plugin.py                  # pytest hooks, JSON test file collection (JsonModule), chain-contiguity ordering hooks
├── factory.py                 # Collection-time test-class factory (create_test_class)
├── carrier.py                 # Runtime execution engine (Carrier class): chain state, iteration matrix, threading, reporting
├── request_builder.py         # Resolved models -> httpx kwargs (build_client_kwargs, build_request_kwargs)
├── response_steps.py          # Meaning of a single verify/save step (process_verify, process_save) — pure, no chain state
├── utils.py                   # Marker construction, substitution processing, scenario-relative path resolution
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

1. **Collection**: `plugin.py:JsonModule.collect()` loads JSON, resolves `$ref`, validates against `Scenario` model, then runs `validation.check_scenario()` which returns coded `Diagnostic` objects — error-severity → `CollectError`, warning-severity → `ScenarioValidationWarning`
2. **Class generation**: `factory.py:create_test_class()` creates dynamic test class with stage methods
3. **Execution**: Each stage method calls `Carrier.execute_stage()` which:
   - Processes substitutions into context
   - Walks request model through template engine, then `request_builder.build_request_kwargs()`
   - Executes HTTP request via httpx
   - Processes response steps via `response_steps.process_verify()` / `process_save()`
   - Updates global context with saved values

Per-scenario mutable class state (client, abort flag, exchange bookkeeping) is defined once in `carrier.fresh_scenario_state()`; the factory seeds each generated subclass with it and `teardown_class` re-applies it.

## Test suite conventions

**Unit vs integration.** `tests/unit` owns pure logic — models, templates, jsonref,
validation, and the error/edge paths of the engine. `tests/integration` owns
everything that needs a real pytest session and a real socket: collection,
ordering, fixtures, marks, and the HTTP round trip. Unit tests alone reach ~87%
coverage in seconds; integration carries the rest to ~97%. When a behavior can
be pinned in a unit test, pin it there — reach for an integration test when the
thing under test *is* the pytest or HTTP interaction.

**Integration tests** use pytest's `pytester` fixture. Prefer the `run_scenario`
fixture (`tests/integration/conftest.py`) over calling `copy_example` /
`runpytest` by hand; it takes extra pytest args via `args=` and switches to a
real subprocess via `subprocess=True`, so almost every case fits:

```python
run_scenario("verify/test_verify_status.http.json")  # plain run
run_scenario("auth/test_request_auth.http.json", "auth.py")  # with aux files
run_scenario("save/test_save_jmespath.http.json", args="--collect-only")
run_scenario(*CHAIN_SCENARIOS, args=("-n", "2"), subprocess=True)
```

**Where a scenario lives.** A scenario that is *fixture scaffolding* for the
behavior under test goes in `tests/integration/examples/` as a real
`test_<name>.http.json`, so the CLI validator and the schema check cover it too.
A scenario that *is* the subject of the test — where reading it next to the
assertion is the point — may be built inline and written into the pytester dir.
Don't add an example file that only one test will ever use inline-style, and
don't inline a scenario other tests could share.

**`slow` marker.** Applies to every test that spawns a pytester subprocess or
waits on a real timeout/rate limit. Keep it accurate: it is what makes
`-m "not slow"` a usable inner loop. CI runs everything.

**"M\<n\>" in docstrings** (`M1`, `M14`, `M50`, ...) are internal review-round
finding IDs, kept as provenance for regression guards. They are not resolvable
outside the review that produced them, so they belong in a docstring next to a
real explanation — never in a test's name, and never as the only thing a
docstring says.
