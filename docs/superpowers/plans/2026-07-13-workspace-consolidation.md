# Workspace Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the six-distribution uv workspace into a single `pytest-httpchain` distribution with domain subpackages, per the approved spec at `docs/superpowers/specs/2026-07-13-workspace-consolidation-design.md`.

**Architecture:** Move each `packages/pytest-httpchain-X` into `src/pytest_httpchain/X` one at a time, bottom-up-safe order (models, jsonref, templates, userfunc, then core last), keeping the full test suite green after every task. No behavior changes — this is packaging and layout only. An import-linter contract then enforces the layering that the workspace used to enforce structurally.

**Tech Stack:** Python ≥3.13, uv (single project, no workspace), uv_build backend, pytest 9, ruff, ty, import-linter.

## Global Constraints

- No behavior changes: `pytest-httpchain schema` output must be byte-identical before and after (verified in Task 9).
- Every task ends with: `uv run pytest -q` green (same test count as baseline), `uv run ruff check .` clean, `uvx ty@0.0.49 check` clean, then a commit.
- `requires-python = ">=3.13"`, build backend `uv_build>=0.7.21,<0.8.0`, ruff line-length 180 — all unchanged.
- Work happens on the `workspace-consolidation` branch (already created; spec committed there).
- Layering rule (normative, from spec): `jsonref`, `templates`, `userfunc` may import only `pytest_httpchain.errors` (+ their own subpackage); `models` may additionally import `templates` and `userfunc`; plugin modules may import anything.
- Commit messages end with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: Record baseline; rename `exceptions.py` → `errors.py`

**Files:**
- Rename: `src/pytest_httpchain/exceptions.py` → `src/pytest_httpchain/errors.py`
- Modify: every file importing it (found by grep in Step 3; expected: `src/pytest_httpchain/carrier.py`, `src/pytest_httpchain/utils.py`, plus any `tests/unit/*.py`)

**Interfaces:**
- Produces: `pytest_httpchain.errors` module exporting `HttpChainError` (re-exported from `pytest_httpchain_core` for now — Task 6 inlines it), `StageExecutionError`, `RequestError`, `SaveError`, `VerificationError`. All later tasks import the base as `from pytest_httpchain.errors import HttpChainError`.

- [ ] **Step 1: Record the green baseline**

Run: `uv run pytest -q 2>&1 | tail -2`
Expected: all passed, 0 failures. Note the total test count — call it `BASELINE` (expected 932); every later task must preserve it.

- [ ] **Step 2: Rename the module**

```bash
git mv src/pytest_httpchain/exceptions.py src/pytest_httpchain/errors.py
```

- [ ] **Step 3: Update importers**

Find them: `grep -rln "from .exceptions import\|from pytest_httpchain.exceptions import\|pytest_httpchain\.exceptions" src tests`

```bash
grep -rl "from \.exceptions import" src | xargs sed -i "s/from \.exceptions import/from .errors import/g"
grep -rl "pytest_httpchain\.exceptions" src tests | xargs sed -i "s/pytest_httpchain\.exceptions/pytest_httpchain.errors/g"
```

Then verify nothing is left: `grep -rn "pytest_httpchain.exceptions\|from .exceptions" src tests` → no output.
(`errors.py` keeps its existing `from pytest_httpchain_core import HttpChainError` line — do NOT change it in this task.)

- [ ] **Step 4: Verify green**

Run: `uv run pytest -q 2>&1 | tail -2 && uv run ruff check . && uvx ty@0.0.49 check`
Expected: BASELINE tests pass; ruff and ty clean.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Rename exceptions.py to errors.py ahead of workspace consolidation"
```

---

### Task 2: Fold `pytest-httpchain-models` into `pytest_httpchain.models`

**Files:**
- Move: `packages/pytest-httpchain-models/src/pytest_httpchain_models/` → `src/pytest_httpchain/models/`
- Move: `packages/pytest-httpchain-models/tests/` → `tests/unit/models/`
- Move: `packages/pytest-httpchain-models/CLAUDE.md` → `src/pytest_httpchain/models/CLAUDE.md`
- Delete: rest of `packages/pytest-httpchain-models/` (pyproject.toml, README.md)
- Modify: `pyproject.toml` (deps, uv.sources, pythonpath), all importers repo-wide

**Interfaces:**
- Produces: `pytest_httpchain.models` subpackage; import form `from pytest_httpchain.models import Scenario` (and `.entities` / `.types` submodules). Tasks 3-6 rely on this form existing.
- Consumes: nothing new. The moved code's imports of `pytest_httpchain_templates` / `pytest_httpchain_userfunc` still resolve against the remaining workspace members — they are rewritten in Tasks 4-5.

- [ ] **Step 1: Move code, tests, CLAUDE.md; delete the package shell**

```bash
git mv packages/pytest-httpchain-models/src/pytest_httpchain_models src/pytest_httpchain/models
mkdir -p tests/unit/models && git mv packages/pytest-httpchain-models/tests/* tests/unit/models/
git mv packages/pytest-httpchain-models/CLAUDE.md src/pytest_httpchain/models/CLAUDE.md
git rm -r packages/pytest-httpchain-models
```

- [ ] **Step 2: Rewrite the module name repo-wide**

```bash
grep -rl "pytest_httpchain_models" --include="*.py" --include="*.md" src tests scripts packages docs 2>/dev/null | xargs -r sed -i "s/pytest_httpchain_models/pytest_httpchain.models/g"
```

Verify: `grep -rn "pytest_httpchain_models" . --exclude-dir=.git --exclude-dir=.venv --exclude=uv.lock --exclude=CHANGELOG.md` → no output.

- [ ] **Step 3: Update `pyproject.toml`**

Remove these three lines (Edit, exact strings):
- from `[project] dependencies`: `    "pytest-httpchain-models",`
- from `[tool.uv.sources]`: `pytest-httpchain-models = { workspace = true }`
- from `[tool.pytest] pythonpath`: `    "packages/pytest-httpchain-models/src",`

Add to `[project] dependencies` (models' external deps not yet at root; jmespath/jsonschema already present):
```toml
    "graphql-core>=3.0.0",
```

- [ ] **Step 4: Relock, sync (uninstalls the old workspace member), verify green**

Run: `uv lock && uv sync --all-extras && uv run pytest -q 2>&1 | tail -2 && uv run ruff check . && uvx ty@0.0.49 check`
Expected: BASELINE tests pass (the models suite now runs from `tests/unit/models/`); ruff/ty clean. `uv sync` must show `pytest-httpchain-models` removed from the environment — this guarantees the sed missed nothing (a leftover import would now fail).

- [ ] **Step 5: Update the moved CLAUDE.md's "Running Tests" section**

In `src/pytest_httpchain/models/CLAUDE.md`, replace the test commands block with:
```bash
uv run pytest tests/unit/models -v
```
and delete any "or from package directory" alternative.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "Fold pytest-httpchain-models into pytest_httpchain.models"
```

---

### Task 3: Fold `pytest-httpchain-jsonref` into `pytest_httpchain.jsonref`

**Files:**
- Move: `packages/pytest-httpchain-jsonref/src/pytest_httpchain_jsonref/` → `src/pytest_httpchain/jsonref/` (keeps `plumbing/`)
- Move: `packages/pytest-httpchain-jsonref/tests/` → `tests/unit/jsonref/` (includes `case_*.json` data dirs and `conftest.py`)
- Move: `packages/pytest-httpchain-jsonref/CLAUDE.md` → `src/pytest_httpchain/jsonref/CLAUDE.md`
- Delete: rest of `packages/pytest-httpchain-jsonref/`
- Modify: `pyproject.toml`, importers repo-wide (`src/pytest_httpchain/plugin.py`, `validation.py`, `cli.py`)

**Interfaces:**
- Produces: `pytest_httpchain.jsonref` subpackage; import form `from pytest_httpchain.jsonref import load_json, ReferenceResolverError`.

- [ ] **Step 1: Move and delete shell**

```bash
git mv packages/pytest-httpchain-jsonref/src/pytest_httpchain_jsonref src/pytest_httpchain/jsonref
mkdir -p tests/unit/jsonref && git mv packages/pytest-httpchain-jsonref/tests/* tests/unit/jsonref/
git mv packages/pytest-httpchain-jsonref/CLAUDE.md src/pytest_httpchain/jsonref/CLAUDE.md
git rm -r packages/pytest-httpchain-jsonref
```

- [ ] **Step 2: Rewrite module name repo-wide**

```bash
grep -rl "pytest_httpchain_jsonref" --include="*.py" --include="*.md" src tests scripts packages docs 2>/dev/null | xargs -r sed -i "s/pytest_httpchain_jsonref/pytest_httpchain.jsonref/g"
```

Verify: `grep -rn "pytest_httpchain_jsonref" . --exclude-dir=.git --exclude-dir=.venv --exclude=uv.lock --exclude=CHANGELOG.md` → no output.

- [ ] **Step 3: Update `pyproject.toml`**

Remove:
- dependency line `    "pytest-httpchain-jsonref",`
- `[tool.uv.sources]` line `pytest-httpchain-jsonref = { workspace = true }`
- pythonpath line `    "packages/pytest-httpchain-jsonref/src",`

Add to `[project] dependencies` (jsonref's external dep):
```toml
    "deepmerge>=2.0",
```

- [ ] **Step 4: Relock, sync, verify green**

Run: `uv lock && uv sync --all-extras && uv run pytest -q 2>&1 | tail -2 && uv run ruff check . && uvx ty@0.0.49 check`
Expected: BASELINE tests pass (jsonref suite runs from `tests/unit/jsonref/`; `pytest-datadir` fixtures find the moved `case_*` dirs because they sit beside the moved test modules); ruff/ty clean.

- [ ] **Step 5: Update `src/pytest_httpchain/jsonref/CLAUDE.md` test commands** to `uv run pytest tests/unit/jsonref -v` (same edit pattern as Task 2 Step 5).

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "Fold pytest-httpchain-jsonref into pytest_httpchain.jsonref"
```

---

### Task 4: Fold `pytest-httpchain-templates` into `pytest_httpchain.templates`

**Files:**
- Move: `packages/pytest-httpchain-templates/src/pytest_httpchain_templates/` → `src/pytest_httpchain/templates/`
- Move: `packages/pytest-httpchain-templates/tests/` → `tests/unit/templates/`
- Move: `packages/pytest-httpchain-templates/CLAUDE.md` → `src/pytest_httpchain/templates/CLAUDE.md`
- Delete: rest of `packages/pytest-httpchain-templates/`
- Modify: `pyproject.toml`, importers repo-wide (includes `src/pytest_httpchain/models/types.py` — the sed catches it)

**Interfaces:**
- Produces: `pytest_httpchain.templates` subpackage; import form `from pytest_httpchain.templates import walk, TemplatesError, TEMPLATE_PATTERN, TEMPLATE_BUILTINS, is_complete_template, extract_template_expression`.

- [ ] **Step 1: Move and delete shell**

```bash
git mv packages/pytest-httpchain-templates/src/pytest_httpchain_templates src/pytest_httpchain/templates
mkdir -p tests/unit/templates && git mv packages/pytest-httpchain-templates/tests/* tests/unit/templates/
git mv packages/pytest-httpchain-templates/CLAUDE.md src/pytest_httpchain/templates/CLAUDE.md
git rm -r packages/pytest-httpchain-templates
```

- [ ] **Step 2: Rewrite module name repo-wide**

```bash
grep -rl "pytest_httpchain_templates" --include="*.py" --include="*.md" src tests scripts packages docs 2>/dev/null | xargs -r sed -i "s/pytest_httpchain_templates/pytest_httpchain.templates/g"
```

Verify: `grep -rn "pytest_httpchain_templates" . --exclude-dir=.git --exclude-dir=.venv --exclude=uv.lock --exclude=CHANGELOG.md` → no output.

- [ ] **Step 3: Update `pyproject.toml`**

Remove:
- dependency line `    "pytest-httpchain-templates",`
- `[tool.uv.sources]` line `pytest-httpchain-templates = { workspace = true }`
- pythonpath line `    "packages/pytest-httpchain-templates/src",`

No dependency additions — `simpleeval>=1.0.3` is already declared at root.

- [ ] **Step 4: Relock, sync, verify green**

Run: `uv lock && uv sync --all-extras && uv run pytest -q 2>&1 | tail -2 && uv run ruff check . && uvx ty@0.0.49 check`
Expected: BASELINE tests pass; ruff/ty clean.

- [ ] **Step 5: Update `src/pytest_httpchain/templates/CLAUDE.md` test commands** to `uv run pytest tests/unit/templates -v`.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "Fold pytest-httpchain-templates into pytest_httpchain.templates"
```

---

### Task 5: Fold `pytest-httpchain-userfunc` into a single `pytest_httpchain/userfunc.py` module

**Files:**
- Create: `src/pytest_httpchain/userfunc.py` (merger of the package's `userfunc.py` + `exceptions.py` + `__init__.py` docstring/`__all__`)
- Move: `packages/pytest-httpchain-userfunc/tests/` → `tests/unit/userfunc/`
- Delete: all of `packages/pytest-httpchain-userfunc/` (its CLAUDE.md content folds into the module docstring)
- Modify: `pyproject.toml`, importers repo-wide (`src/pytest_httpchain/models/types.py`, `src/pytest_httpchain/utils.py`, `src/pytest_httpchain/carrier.py`, `src/pytest_httpchain/validation.py`)

**Interfaces:**
- Produces: `pytest_httpchain.userfunc` **module** exporting `NAME_PATTERN`, `import_function`, `call_function`, `wrap_function`, `UserFunctionError`. External import form is unchanged apart from the dot: `from pytest_httpchain.userfunc import call_function`. (Verified: no external code imports `pytest_httpchain_userfunc.<submodule>` paths, so flattening cannot break any importer.)

- [ ] **Step 1: Create the flattened module**

Create `src/pytest_httpchain/userfunc.py` as follows, then delete the package:
1. Start from `packages/pytest-httpchain-userfunc/src/pytest_httpchain_userfunc/userfunc.py` (copy content verbatim).
2. Replace its module docstring with the package `__init__.py` docstring (the one beginning "User function handling for pytest-httpchain."), and append to that docstring the "Key Behaviors" section of the package CLAUDE.md (wrap_function merging rule + the error-message-carries-cause contract paragraph).
3. Replace the line `from .exceptions import UserFunctionError` with:

```python
from pytest_httpchain_core import HttpChainError


class UserFunctionError(HttpChainError):
    """Error importing or calling a user-supplied function."""
```

(Task 6's sed rewrites the `pytest_httpchain_core` import to `pytest_httpchain.errors`.)
4. Add at the end of the imports/class block, preserving the package's public surface:

```python
__all__ = [
    "NAME_PATTERN",
    "import_function",
    "call_function",
    "wrap_function",
    "UserFunctionError",
]
```

```bash
mkdir -p tests/unit/userfunc && git mv packages/pytest-httpchain-userfunc/tests/* tests/unit/userfunc/
git rm -r packages/pytest-httpchain-userfunc
git add src/pytest_httpchain/userfunc.py
```

- [ ] **Step 2: Rewrite module name repo-wide**

```bash
grep -rl "pytest_httpchain_userfunc" --include="*.py" --include="*.md" src tests scripts docs 2>/dev/null | xargs -r sed -i "s/pytest_httpchain_userfunc/pytest_httpchain.userfunc/g"
```

Then check for now-invalid submodule paths introduced by the sed (expected only inside `tests/unit/userfunc/` if its tests imported `.userfunc`/`.exceptions`/`._helpers` submodules):

```bash
grep -rn "pytest_httpchain\.userfunc\.\(userfunc\|exceptions\)" src tests
```

For every hit, collapse the path: `pytest_httpchain.userfunc.userfunc` → `pytest_httpchain.userfunc` and `pytest_httpchain.userfunc.exceptions` → `pytest_httpchain.userfunc`. If `tests/unit/userfunc/_helpers.py` is imported as `_helpers` (directory-relative), it keeps working — pytest's importlib mode resolves it beside the test module; leave it.

Verify: `grep -rn "pytest_httpchain_userfunc" . --exclude-dir=.git --exclude-dir=.venv --exclude=uv.lock --exclude=CHANGELOG.md` → no output.

- [ ] **Step 3: Update `pyproject.toml`**

Remove:
- dependency line `    "pytest-httpchain-userfunc",`
- `[tool.uv.sources]` line `pytest-httpchain-userfunc = { workspace = true }`
- pythonpath line `    "packages/pytest-httpchain-userfunc/src",`

No dependency additions — the package had none.

- [ ] **Step 4: Relock, sync, verify green**

Run: `uv lock && uv sync --all-extras && uv run pytest -q 2>&1 | tail -2 && uv run ruff check . && uvx ty@0.0.49 check`
Expected: BASELINE tests pass; ruff/ty clean.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Fold pytest-httpchain-userfunc into pytest_httpchain/userfunc.py"
```

---

### Task 6: Fold core, delete the workspace, finish `pyproject.toml`

**Files:**
- Modify: `src/pytest_httpchain/errors.py` (inline `HttpChainError`)
- Modify: `src/pytest_httpchain/jsonref/exceptions.py`, `src/pytest_httpchain/templates/exceptions.py`, `src/pytest_httpchain/userfunc.py` (base-class import)
- Delete: `packages/` entirely (only `pytest-httpchain-core` remains in it)
- Modify: `pyproject.toml` (workspace tables, testpaths, coverage, ty overrides), `Makefile`

**Interfaces:**
- Produces: `pytest_httpchain.errors.HttpChainError` as the sole base-exception definition; a workspace-free single-project `pyproject.toml`. Every subsequent task assumes `packages/` no longer exists.

- [ ] **Step 1: Inline the base exception in `errors.py`**

Replace the line `from pytest_httpchain_core import HttpChainError` in `src/pytest_httpchain/errors.py` with:

```python
class HttpChainError(Exception):
    """Base exception for all pytest-httpchain errors."""
```

(placed above `StageExecutionError`, which subclasses it).

- [ ] **Step 2: Repoint the domain packages' base-class import and delete core**

```bash
grep -rl "pytest_httpchain_core" --include="*.py" src tests | xargs -r sed -i "s/from pytest_httpchain_core import HttpChainError/from pytest_httpchain.errors import HttpChainError/g"
git rm -r packages
```

Verify: `grep -rn "pytest_httpchain_core" . --exclude-dir=.git --exclude-dir=.venv --exclude=uv.lock --exclude=CHANGELOG.md` → no output, and `packages/` is gone.

- [ ] **Step 3: Finish `pyproject.toml`**

Remove entirely:
- dependency line `    "pytest-httpchain-core",`
- the whole `[tool.uv.workspace]` table (`members = ["packages/*"]`)
- the whole `[tool.uv.sources]` table (now empty of entries)
- pythonpath line `    "packages/pytest-httpchain-core/src",`

Change:
- `[tool.pytest]`: `testpaths = ["tests", "packages/*/tests"]` → `testpaths = ["tests"]`
- `[tool.coverage.run]`: the six-entry `source` list → `source = ["src"]`
- `[[tool.ty.overrides]]`: `include = ["tests/**", "packages/*/tests/**"]` → `include = ["tests/**"]`

In `Makefile`, change `uv sync --all-extras --all-packages` → `uv sync --all-extras`.

- [ ] **Step 4: Relock, sync, verify green**

Run: `uv lock && uv sync --all-extras && uv run pytest -q 2>&1 | tail -2 && uv run ruff check . && uvx ty@0.0.49 check`
Expected: BASELINE tests pass; ruff/ty clean; `uv.lock` no longer lists any `pytest-httpchain-*` sub-distribution.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Fold pytest-httpchain-core into errors.py and dissolve the uv workspace"
```

---

### Task 7: Enforce layering with import-linter

**Files:**
- Modify: `pyproject.toml` (dev dependency + contract), `.github/workflows/test.yml` (lint job)

**Interfaces:**
- Produces: `uv run lint-imports` as a CI-enforced gate. The contract below IS the spec's layering rule; later refactors must keep it passing.

- [ ] **Step 1: Add the dev dependency and contract**

Add `"import-linter>=2.0",` to `[dependency-groups] dev` in `pyproject.toml`, then append:

```toml
[tool.importlinter]
root_package = "pytest_httpchain"

[[tool.importlinter.contracts]]
name = "domain subpackages do not import plugin modules"
type = "forbidden"
source_modules = [
    "pytest_httpchain.models",
    "pytest_httpchain.templates",
    "pytest_httpchain.jsonref",
    "pytest_httpchain.userfunc",
]
forbidden_modules = [
    "pytest_httpchain.plugin",
    "pytest_httpchain.carrier",
    "pytest_httpchain.cli",
    "pytest_httpchain.validation",
    "pytest_httpchain.dataflow",
    "pytest_httpchain.schema",
    "pytest_httpchain.utils",
    "pytest_httpchain.har_writer",
    "pytest_httpchain.report_formatter",
    "pytest_httpchain.constants",
]

[[tool.importlinter.contracts]]
name = "lower domain layers do not import models"
type = "forbidden"
source_modules = [
    "pytest_httpchain.templates",
    "pytest_httpchain.jsonref",
    "pytest_httpchain.userfunc",
]
forbidden_modules = ["pytest_httpchain.models"]
```

- [ ] **Step 2: Run it**

Run: `uv lock && uv sync --all-extras && uv run lint-imports`
Expected: `Contracts: 2 kept, 0 broken.` If a contract breaks, the offending import violates the spec's layering rule — fix the import, do not weaken the contract.

- [ ] **Step 3: Add to CI lint job**

In `.github/workflows/test.yml`, after the "Check types" step of the `lint` job, add:

```yaml
            - name: Check import layering
              run: uv run lint-imports
```

- [ ] **Step 4: Verify green and commit**

Run: `uv run pytest -q 2>&1 | tail -2 && uv run ruff check .`
Expected: BASELINE tests pass.

```bash
git add -A && git commit -m "Enforce domain-subpackage layering with import-linter"
```

---

### Task 8: Distribution metadata and CI/publish commands

**Files:**
- Create: `src/pytest_httpchain/py.typed` (empty)
- Modify: `pyproject.toml` (`[project.urls]`), `.github/workflows/test.yml` (test command), `.github/workflows/publish.yml` (build command)

**Interfaces:**
- Produces: a wheel that carries `py.typed` and correct project URLs; CI that no longer references `packages/`.

- [ ] **Step 1: Add `py.typed` and URLs**

```bash
touch src/pytest_httpchain/py.typed && git add src/pytest_httpchain/py.typed
```

Add to `pyproject.toml` after `classifiers`:

```toml
[project.urls]
Repository = "https://github.com/aeresov/pytest-httpchain"
Documentation = "https://aeresov.github.io/pytest-httpchain/"
Changelog = "https://github.com/aeresov/pytest-httpchain/blob/main/CHANGELOG.md"
```

(Verify the two hostnames against `mkdocs.yml`'s `site_url`/`repo_url` before committing; use those values if they differ.)

- [ ] **Step 2: Update CI test command**

In `.github/workflows/test.yml` line ~64, change:
`uv run pytest tests/unit tests/integration packages/*/tests -v --cov ...` → `uv run pytest tests -v --cov --cov-report=xml --cov-report=term-missing`

- [ ] **Step 3: Update publish build command**

In `.github/workflows/publish.yml`, change `uv build --all-packages` → `uv build` (keep the `ls -la dist/` echo).

- [ ] **Step 4: Verify the wheel**

Run: `rm -rf dist && uv build && ls dist/ && unzip -l dist/pytest_httpchain-*.whl | grep -E "py.typed|models/|templates/|jsonref/|userfunc" | head`
Expected: exactly one wheel + one sdist; wheel lists `pytest_httpchain/py.typed`, `pytest_httpchain/models/...`, `pytest_httpchain/templates/...`, `pytest_httpchain/jsonref/...`, `pytest_httpchain/userfunc.py`. No other top-level package.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Add py.typed and project URLs; update CI/publish for single distribution"
```

---

### Task 9: Docs sweep and schema regeneration

**Files:**
- Modify: `CLAUDE.md`, `README.md`, `docs/schema/scenario.schema.json` (regenerated), `scripts/generate_schema.py` (only if the Task 2-5 seds missed it)

**Interfaces:**
- Produces: repo docs that describe the single-distribution layout; proof of the no-behavior-change constraint (byte-identical schema).

- [ ] **Step 1: Rewrite root `CLAUDE.md`**

Update these specific parts:
- The "Commands" section: delete the four `uv run pytest packages/...-*/tests -v` lines; delete the "(may have conftest conflicts, use specific paths)" caveat — replace the all-tests line with plain `uv run pytest`.
- The "Architecture" tree: replace the `packages/` block with the new `src/pytest_httpchain/` tree from the spec (models/, templates/, jsonref/, userfunc.py, errors.py), and change "This is a uv workspace monorepo with the main plugin in `src/` and supporting packages in `packages/`" to "The plugin is a single distribution; domain subpackages (models, templates, jsonref, userfunc) live under `src/pytest_httpchain/` and must not import plugin modules (enforced by import-linter)."
- The sentence "Most packages have their own CLAUDE.md..." → "The models, templates, and jsonref subpackages carry their own CLAUDE.md next to their code."
- `exceptions.py` reference → `errors.py`.

- [ ] **Step 2: Sweep README and docs/**

Run: `grep -rn "pytest_httpchain_\|pytest-httpchain-\|packages/" README.md docs/*.md docs/*/*.md mkdocs.yml`
Expected: near-zero hits (pre-verified: docs contain none). Fix any that appear (install instructions naming sub-packages, monorepo mentions).

- [ ] **Step 3: Regenerate the JSON schema and prove it unchanged**

Run: `uv run python scripts/generate_schema.py > /dev/null 2>&1 || uv run pytest-httpchain schema > docs/schema/scenario.schema.json; git diff --stat docs/schema/scenario.schema.json`
Expected: **no diff** (models unchanged ⇒ byte-identical schema). A diff here means a behavior change slipped in — stop and investigate before proceeding. First check how `scripts/generate_schema.py` is meant to be invoked (read it) and use its native invocation.

- [ ] **Step 4: Verify green and commit**

Run: `uv run pytest -q 2>&1 | tail -2 && uv run ruff check .`

```bash
git add -A && git commit -m "Update docs for single-distribution layout"
```

---

### Task 10: Version 0.9.0, changelog, final verification

**Files:**
- Modify: `pyproject.toml` (version), `CHANGELOG.md`

**Interfaces:**
- Produces: a releasable branch. PyPI release + yank are manual post-merge steps (below), not part of this plan.

- [ ] **Step 1: Bump version**

In `pyproject.toml`: `version = "0.8.1"` → `version = "0.9.0"`. Run `uv lock`.

- [ ] **Step 2: Changelog entry**

Add at the top of `CHANGELOG.md` under a `## 0.9.0` heading:

```markdown
### Changed (breaking)

- Consolidated the six-distribution uv workspace into the single `pytest-httpchain`
  distribution. The former sub-packages are now subpackages/modules:
  `pytest_httpchain.models`, `pytest_httpchain.templates`, `pytest_httpchain.jsonref`,
  `pytest_httpchain.userfunc`; the shared exception base lives in
  `pytest_httpchain.errors`. Migration: install `pytest-httpchain>=0.9` only (drop any
  explicit `pytest-httpchain-*` requirements) and rename imports
  `pytest_httpchain_X` → `pytest_httpchain.X`. Scenario JSON files, user-function
  references, ini options, and CLI usage are unaffected.
- Distribution now ships a `py.typed` marker and `[project.urls]` metadata.
- Import layering between domain subpackages and plugin modules is enforced by
  import-linter in CI.
```

- [ ] **Step 3: Full final verification**

```bash
uv run pytest -q 2>&1 | tail -2          # BASELINE tests pass
uv run ruff check . && uv run ruff format --check .
uvx ty@0.0.49 check && uv run lint-imports
rm -rf dist && uv build && ls dist/       # one wheel + one sdist, version 0.9.0
grep -rn "pytest_httpchain_\|pytest-httpchain-core\|pytest-httpchain-models\|pytest-httpchain-templates\|pytest-httpchain-jsonref\|pytest-httpchain-userfunc" . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=dist --exclude=uv.lock --exclude=CHANGELOG.md
```

Expected: last grep returns **no output** (CHANGELOG excluded — history may mention old names).

- [ ] **Step 4: Scratch-install smoke test**

```bash
cd "$(mktemp -d)" && uv venv -q && uv pip install -q /home/aeresov/workshop/aeresov/pytest-httpchain/dist/pytest_httpchain-0.9.0-py3-none-any.whl pytest
printf '{"stages": [{"name": "s", "request": {"url": "https://example.com"}}]}' > test_smoke.http.json
.venv/bin/pytest-httpchain validate test_smoke.http.json && .venv/bin/pytest --collect-only -q test_smoke.http.json
cd - >/dev/null
```

Expected: `validate` prints `test_smoke.http.json: OK` (exit 0); pytest collects 1 test from the scenario. (Collection only — no network request is made... note: per the known collection-side-effects issue, collection builds an httpx.Client but sends nothing; that is current, unchanged behavior.)

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Release 0.9.0: single-distribution consolidation"
```

---

## Manual post-merge steps (not plan tasks)

1. Merge `workspace-consolidation` → `main`; create the 0.9.0 GitHub release (triggers publish.yml → PyPI, now one distribution).
2. Notify internal users: `pip install -U "pytest-httpchain>=0.9"`, drop `pytest-httpchain-*` requirement entries, rename direct imports.
3. After migration confirmed: yank **all** releases of `pytest-httpchain-core`, `-jsonref`, `-models`, `-templates`, `-userfunc` on PyPI (web UI/API, per release). Yank is reversible.
