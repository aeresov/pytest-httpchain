# Workspace consolidation: six distributions → one

**Date:** 2026-07-13
**Status:** Approved design, pending implementation plan
**Ships as:** 0.9.0 (0.x convention: breaking change = minor bump)

## Motivation

The uv-workspace split into six PyPI distributions (`pytest-httpchain` + `-core`, `-jsonref`, `-models`, `-templates`, `-userfunc`) is legacy. All consumers are internal, install the full plugin, and are easy to update. The split's costs are real and were confirmed by the 2026-07 design review: inter-package dependencies published unpinned, `pydantic` undeclared in two packages, six metadata surfaces (license/urls/py.typed missing), lockstep six-way version bumps, and a publish flow that can leave PyPI in a mixed-version state. The module *boundaries* are good; the *distribution* boundaries pay no rent.

## Decisions (confirmed with owner)

1. **Consumers:** plugin-only today; no standalone users of sub-packages.
2. **Future independence:** fold everything now, keep extraction cheap via preserved boundaries + an enforced layering rule ("fold all, extract later").
3. **Old PyPI names:** yank all releases of the five sub-package names after 0.9.0 ships and internal users migrate.

## Target layout

```
src/pytest_httpchain/
├── __init__.py            # public API re-exports (unchanged names, new import paths)
├── py.typed               # NEW — single marker for the whole distribution
├── errors.py              # HttpChainError (was -core) + StageExecutionError family (was exceptions.py)
├── constants.py  plugin.py  carrier.py  cli.py
├── validation.py  dataflow.py  schema.py
├── utils.py  har_writer.py  report_formatter.py
├── models/                # was packages/pytest-httpchain-models (entities.py, types.py, __init__.py — has no exceptions module; raises pydantic ValidationError)
├── templates/             # was packages/pytest-httpchain-templates (expressions.py, substitution.py, exceptions.py, __init__.py)
├── jsonref/               # was packages/pytest-httpchain-jsonref (loader.py, plumbing/, exceptions.py, __init__.py)
└── userfunc.py            # was packages/pytest-httpchain-userfunc — flattened to one module (117 lines + exception)

tests/
├── unit/                  # existing flat plugin-module tests stay
│   ├── jsonref/           # moved from packages/pytest-httpchain-jsonref/tests (incl. case_*.json dirs, conftest)
│   ├── models/            # moved from packages/pytest-httpchain-models/tests
│   ├── templates/         # moved from packages/pytest-httpchain-templates/tests
│   └── userfunc/          # moved from packages/pytest-httpchain-userfunc/tests
└── integration/           # unchanged
```

`packages/` is deleted. No top-level module name collides with the new subpackage names (verified against current `src/pytest_httpchain/`).

### Imports

Mechanical rename across src, tests, scripts, docs: `pytest_httpchain_models` → `pytest_httpchain.models`, `pytest_httpchain_templates` → `pytest_httpchain.templates`, `pytest_httpchain_jsonref` → `pytest_httpchain.jsonref`, `pytest_httpchain_userfunc` → `pytest_httpchain.userfunc`, `pytest_httpchain_core.exceptions` → `pytest_httpchain.errors`. Internal `from .exceptions import …` in plugin modules becomes `from .errors import …`.

### Exceptions

`errors.py` holds the base `HttpChainError` and the plugin-side hierarchy (`StageExecutionError`, `RequestError`, `SaveError`, `VerificationError`). Domain subpackages keep their own `exceptions.py` (`ReferenceResolverError`, `TemplatesError`; `UserFunctionError` lives inside `userfunc.py` after flattening), subclassing the base from `pytest_httpchain.errors`. Rationale: an extracted package carries its error type with it and re-declares only the one-line base.

### Layering rule (the extract-later guardrail)

Domain subpackages must not import plugin modules. Allowed dependencies:

- `jsonref`, `templates`, `userfunc` → `errors` only (plus their own subpackage)
- `models` → `templates`, `userfunc`, `errors`
- plugin modules (everything else at top level) → anything

Enforced with **import-linter** (dev dependency): a layers/forbidden contract in `pyproject.toml`, run as `lint-imports` in the CI lint job. Exact contract syntax is an implementation detail; the rule above is normative.

## Packaging and tooling

**`pyproject.toml` (single, root):**
- `dependencies`: remove the five `pytest-httpchain-*` entries; add `deepmerge>=2.0` (from jsonref) and `graphql-core>=3.0.0` (from models). Everything else already declared at root (`pydantic` included — closing the undeclared-dependency defect for good).
- Delete `[tool.uv.workspace]` and `[tool.uv.sources]`; regenerate `uv.lock`.
- Add `[project.urls]` (Repository, Documentation, Changelog).
- Build backend (`uv_build`), `pytest11` entry point, console script: unchanged.
- `[tool.pytest]`: `pythonpath` drops the five `packages/*/src` entries; `testpaths = ["tests"]`.
- `[tool.coverage.run]`: `source = ["src"]`.
- `[[tool.ty.overrides]]` include: `["tests/**"]`.
- Add import-linter to the `dev` dependency group.

**CI (`.github/workflows/test.yml`):** test command becomes `uv run pytest tests -v --cov …` (drop `packages/*/tests`); lint job gains `uv run lint-imports`.

**Publish (`.github/workflows/publish.yml`):** `uv build --all-packages` → `uv build`. (The missing test-gate on publish is a known separate follow-up, deliberately not bundled here.)

## Tests

Suites move wholesale; only imports change inside the files. Per-package `conftest.py` files move with their suites (pytest scopes them to their directory). `--import-mode=importlib` is already set, so module-name collisions between moved suites and existing `tests/unit/test_*.py` files are impossible. `pytest-datadir` (used by the jsonref suite) is already a root dev dependency.

## Docs

- Per-package `CLAUDE.md` files move next to their code: `src/pytest_httpchain/jsonref/CLAUDE.md`, `src/pytest_httpchain/models/CLAUDE.md`, `src/pytest_httpchain/templates/CLAUDE.md`; the userfunc CLAUDE.md content folds into the `userfunc.py` module docstring (module is now a single file). Internal paths/import examples inside them are updated.
- The five per-package `README.md`s (PyPI-facing) are deleted.
- Root `README.md` and `CLAUDE.md`: remove workspace/monorepo language, per-package test commands, and the stale "conftest conflicts" warning; update the architecture tree.
- `docs/` (mkdocs): sweep for `pytest_httpchain_*` import examples and monorepo references.
- `CHANGELOG.md`: one 0.9.0 entry documenting the consolidation and the import-path migration.
- `scripts/generate_schema.py`: imports updated; `docs/schema/scenario.schema.json` regenerated (content should be identical — models are unchanged).

## Release and PyPI cleanup

1. Ship 0.9.0 as usual (GitHub release → publish.yml → PyPI), now publishing exactly one distribution.
2. Migrate internal users (below).
3. Yank **every** release of `pytest-httpchain-core`, `-jsonref`, `-models`, `-templates`, `-userfunc` via the PyPI UI/API.

Known consequence, accepted: old `pytest-httpchain<=0.8.1` distributions depend on the yanked names unpinned, so installing an *old* plugin version after the yank may fail resolution. Internal-only users make this acceptable; yanking is reversible if ever needed.

## Internal user migration (communicated once)

1. `pip install -U "pytest-httpchain>=0.9"`; delete any explicit `pytest-httpchain-*` entries from requirements files.
2. If any code imports the libraries directly: rename `pytest_httpchain_X` → `pytest_httpchain.X`.
3. Unaffected: scenario JSON files, user functions (`module:func` strings), pytest ini options, CLI invocations, `$ref`/`$include` behavior.

## Rollback

All changes land as one commit-set on a branch. Nothing is deleted from PyPI (yank is reversible). Rollback = revert the merge and skip/undo the yank.

## Out of scope

Everything else from the 2026-07 design review: collection-time side effects, publish test-gate, xdist story, enum widening, path-resolution unification, ini-option namespacing. This refactor changes *packaging and layout only* — no behavior, no model changes, no new features.

## Success criteria

- `uv run pytest tests` green (all 932 tests, moved suites included) on a clean checkout.
- `uv run ruff check .`, `uvx ty check`, and `uv run lint-imports` green.
- `uv build` produces one wheel + sdist; wheel contains `pytest_httpchain/` only, with `py.typed`.
- A scratch project can `pip install` the built wheel and run a sample scenario + `pytest-httpchain validate`.
- No references to `pytest_httpchain_*` module names or `packages/` remain in the repo (except CHANGELOG history).
