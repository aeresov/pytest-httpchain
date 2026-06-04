# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- New read-only inspection CLI commands: `pytest-httpchain schema` (emit the scenario JSON Schema for editor integration), `resolve` (print a scenario with `$ref`/`$include`/`$merge` inlined), `show` (summarize stages and variable data-flow), and `graph` (emit a Mermaid flowchart of the stage data-flow).

### Removed

- **BREAKING**: Removed the `pytest-httpchain install` command and the bundled skill-installation machinery, including `src/pytest_httpchain/skill.md`. The Claude Code authoring skill now lives in a dedicated Claude Code plugin.

## [0.4.0] - 2026-06-04

### Added

- **Order-aware data-flow validation**: the validator now tracks variable availability stage-by-stage. A variable referenced before the stage that saves it — or referenced in a stage's request when it is only saved in that same stage's response — is reported as a forward reference (`HTTPCHAIN004`), distinct from a plain undefined-variable typo (`HTTPCHAIN003`).
- New semantic checks: a `verify` step that asserts nothing (`HTTPCHAIN006`), and body checks that both require and forbid the same `contains`/`not_contains` substring (`HTTPCHAIN007`, error) or `matches`/`not_matches` pattern (`HTTPCHAIN008`, error).
- Every validation finding now carries a stable diagnostic code (`HTTPCHAINxxx`), a severity, and a source location.
- `pytest-httpchain validate --format json` emits machine-readable diagnostics for editor/CI integration.
- **Deep validation** (opt-in `pytest-httpchain validate --deep`): resolves user-function references (`module:func`) by importing them (`HTTPCHAIN022`), checks call signatures against the arguments each call site provides — including the framework-injected `response` for save/verify functions (`HTTPCHAIN023` unexpected argument, `HTTPCHAIN024` missing required argument) — and verifies that referenced files exist (`HTTPCHAIN020`) and schema files are valid (`HTTPCHAIN021`). Deep findings are warnings; `--syspath` adds import roots and `--strict` makes warnings fail the exit code. Because it imports user code, deep validation never runs at collection time.
- `--strict` flag makes any warning count toward a non-zero exit (useful in CI alongside `--deep`).

### Fixed

- Undefined-variable detection no longer reports comprehension loop variables or lambda parameters (e.g. `x` in `{{ [x for x in items] }}`) as undefined — they are local bindings, not context references.
- The validator now flags `parametrize` parameter *values* that reference stage-level substitutions, fixtures, or saved variables: those values are resolved at collection time against scenario-level substitutions only, so such references fail at runtime. (`parallel.foreach` values, resolved later against the full stage context, are unaffected.)

## [0.3.0] - 2026-06-03

### Added

- `pytest-httpchain validate <file>...` CLI command for validating scenario files (structure plus semantic checks); exits non-zero on failure, so it can be used as a CI gate.
- Semantic validation now runs at **pytest collection time**: semantic errors (duplicate stage names, fixture/variable conflicts) fail collection with a clear message, and issues (undefined variables, stages with no verify) are reported as `ScenarioValidationWarning`. `pytest --collect-only` validates an entire suite.

### Changed

- Scenario validation logic now lives in the main package (`pytest_httpchain.validation`) as the single source of truth.
- `pytest-httpchain install` now installs only the Claude Code skill (the `--skill`/`--mcp` flags are removed).

### Removed

- **BREAKING**: Removed the bundled MCP server — the `pytest-httpchain-mcp` package, the `pytest-httpchain mcp` command, and the `mcp[cli]` dependency. Scenario validation is now available through the `pytest-httpchain validate` CLI command and at pytest collection time.

### Fixed

- Undefined-variable detection no longer emits false positives for names injected by `parametrize`, `parallel.foreach`, or `functions` substitutions.
- Undefined-variable detection now flags references to response data (`response`, `status_code`, `body`, etc.) inside `{{ }}` templates, where they are not available — response values reach templates only via an earlier `save` step.

## [0.2.1] - 2026-01-09

### Added

- Stages can now be defined as a dict with stage names as keys, in addition to the existing list format
  ```json
  // List format (existing)
  { "stages": [{ "name": "login", "request": {...} }] }

  // Dict format (new)
  { "stages": { "login": { "request": {...} } } }
  ```

### Changed

- Stage `name` field is now optional (defaults to empty string)
- Improved type safety in MCP server variable extraction functions
- `CircularDependencyTracker.create_child()` now properly supports subclasses

## [0.2.0] - 2026-01-08

### Changed

- **BREAKING**: Migrated HTTP client from `requests` to `httpx` for improved async support and HTTP/2 capabilities
- **BREAKING**: Scenario format restructured - variables are now defined within `substitutions` array instead of top-level `vars` key
  ```json
  // Before (v0.1.x)
  { "vars": { "user_id": 1 } }
  
  // After
  { "substitutions": [{ "vars": { "user_id": 1 } }] }
  ```
- **BREAKING**: JMESPath extraction in response `save` block now uses `jmespath` key instead of `vars`
  ```json
  // Before (v0.1.x)
  { "save": { "vars": { "user_name": "user.name" } } }
  
  // After  
  { "save": { "jmespath": { "user_name": "user.name" } } }
  ```
- Template engine now powered by `simpleeval` for safer expression evaluation

### Added

- User functions can now be called directly within substitution expressions
- Improved template expression capabilities with `simpleeval` integration

### Removed

- Removed note about parametrization not being implemented (feature now available)

## [0.1.2] - 2025-08-16

### Changed

- Updated package metadata to use `License-Expression: MIT` header for PEP 639 compliance

## [0.1.1] - 2025-08-16

### Changed

- Fixed markdown formatting in README (replaced backslash line breaks with double-space line breaks)

## [0.1.0] - 2025-08-16

### Added

- Initial release
- Declarative JSON test scenario format
- Multi-stage HTTP test support with ordered execution
- Common data context for sharing variables between stages
- Jinja-style template expressions with `{{ variable }}` syntax
- JMESPath support for extracting values from JSON responses
- JSON Schema validation for response verification
- User-defined Python functions for:
  - Custom data extraction
  - Response verification
  - Custom authentication
- JSONRef support with `$ref` directive for scenario reuse
- `always_run` parameter for cleanup stages
- Pytest integration (markers, fixtures, plugins)
- MCP (Model Context Protocol) server for AI code assistant integration
- Optional `mcp` dependency for MCP server installation
- Configurable test file suffix (default: `http`)
- Configurable `$ref` path traversal depth

[Unreleased]: https://github.com/aeresov/pytest-httpchain/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.2.4...v0.3.0
[0.2.1]: https://github.com/aeresov/pytest-httpchain/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/aeresov/pytest-httpchain/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/aeresov/pytest-httpchain/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/aeresov/pytest-httpchain/releases/tag/v0.1.0