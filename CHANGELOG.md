# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Response metadata is now declaratively reachable in response steps via the reserved `response` namespace — `response.status`, `response.reason`, `response.headers` (case-insensitive), `response.elapsed_ms` — usable in `verify.expressions` (`{{ response.status == 200 }}`, `{{ 'json' in response.headers['content-type'] }}`) and in save templates, which makes saving a header a one-liner substitutions save. The namespace exists only inside response steps; the response **body** stays out (extract it with `save`). A user variable/save/fixture named `response` is shadowed there — the validator warns with the new `HTTPCHAIN027` diagnostic.
- `verify.headers` values accept matcher objects — `{"contains": ...}`, `{"not_contains": ...}`, `{"matches": ...}`, `{"not_matches": ...}` — besides the existing exact-match strings, so partial and pattern header assertions no longer require saving the header first. An absent header behaves as an empty string for the matcher forms, mirroring the body-check semantics. Contradictory matchers (same value in `contains` and `not_contains`) are a validation error, like their body counterparts.
- Ambiguous `$ref` lookups are now flagged. A relative reference path is looked up against the referencing file's directory first, then the root path; when a file exists under **both**, the file-relative one wins as before, but the resolver now emits `AmbiguousReferenceWarning` (surfaced by `pytest-httpchain validate` as the new `HTTPCHAIN026` warning diagnostic) instead of silently shadowing the root-relative file. The lookup order itself is now documented in the references guide.

### Changed

- **BREAKING**: `null` no longer bypasses the `$ref`/`$include` sibling-merge conflict rules. Previously a `null` on either side of a merged path was always accepted and the sibling silently won — so a sibling `null` could blank out any referenced value, contradicting the documented no-last-wins guarantee. Now `null` is a value like any other: pairing it with a different value at the same path fails loading with `Merge conflict at <path>` (two `null`s, like any equal values, still merge fine). The whole merge policy is now a single custom `deepmerge.Merger` whose conflict strategies raise, replacing the separate pre-merge conflict detector that had to be kept in sync by hand.

- **BREAKING**: dependency floors raised to match tested reality, now enforced by a lowest-floors CI job that installs every direct dependency at its declared minimum: `pytest>=9.0` (the `[tool.pytest]` configuration table is only read by pytest 9 — under 8.x the plugin mis-collected its own examples and `minversion` was silently unenforced), `pydantic>=2.13.4` (stable generated-schema output), `pyrate-limiter>=4.2.0` (the blocking `try_acquire` API the runner uses).
- Windows is now part of the CI test matrix. One portability fix came out of it: absolute `$ref` paths are judged under both POSIX and Windows path rules on every platform (previously `/etc/passwd` was not recognized as absolute when running on Windows; the root-containment check still applied, but the explicit rejection now matches on all hosts).
- HAR output (`--httpchain-output-dir`) for a parallel stage now contains one entry per iteration, in iteration order, instead of a single arbitrary iteration presented as the stage's only exchange. The `write_har_file` helper accordingly takes a list of `(request, response)` exchanges instead of one pair. (Exchanges are only retained for the HAR when the option is on, so runs without it keep the previous memory footprint.)
- The CLI's default `$ref` resolution root (`--root-path` unset) is now the auto-detected project root — the nearest ancestor containing a standard project marker (`pytest.ini`, `pyproject.toml`, `tox.ini`, `setup.cfg`, `setup.py`, `.git`) — instead of the nearest `tests/` ancestor. This matches pytest collection, which sandboxes `$ref` to pytest's `rootpath`, so `validate`/`show`/`graph` now accept exactly the references that collection accepts; pytest collection itself also routes through the same load pipeline as the CLI. A `$ref` that previously needed an explicit `--root-path` to reach a shared fragment above `tests/` now resolves by default.

### Fixed

- A request that received no response (timeout, connection error) no longer vanishes from the diagnostics: the request that was on the wire is attached to the failure, shown in the report's `HTTP Request` section, and written to the HAR file as a status-`0` entry (the convention browser HAR exports use for aborted requests). Previously the HAR was silently skipped and no request section appeared.
- The report's `HTTP Request`/`HTTP Response` sections for a parallel stage now say which exchange they show — `(failing of N parallel iterations)` or `(last of N parallel iterations)` — instead of presenting one iteration as the stage's only exchange.

## [0.10.0] - 2026-07-16

### Added

- `request.method` accepts any RFC 9110 token, so non-enum verbs — WebDAV `PROPFIND`/`REPORT`, cache `PURGE`, vendor methods — are now representable; the standard verbs keep editor autocomplete. `verify.status` accepts any integer 100-599, so nonstandard codes (nginx 499, 599, vendor codes) can be asserted. Note: the editor schema no longer flags an unknown-but-token-shaped method (e.g. `FOOBAR`) as a typo, since it is now a legal value.
- Namespaced pytest options: `httpchain_suffix`, `httpchain_ref_parent_traversal_depth`, `httpchain_max_comprehension_length`, `httpchain_max_parallel_iterations` ini options and the `--httpchain-output-dir` flag. pytest ini options share one global namespace across all plugins, so the generic old names risked collisions.
- New `info` diagnostic severity: purely informational findings that never affect validity, are exempt from `--strict`, and produce no collection warnings. First code: `HTTPCHAIN025` — a stage's `parametrize` values contain `{{ }}` templates, which opts the scenario into collection-time resolution of scenario substitutions (the one exception to lazy initialization, see below).
- Docs: a "Resolution Phases" reference (collection / scenario initialization / stage execution) in the context-layering guide, describing what resolves in each phase, against which context, and how failures surface.

### Deprecated

- The un-prefixed ini option names (`suffix`, `ref_parent_traversal_depth`, `max_comprehension_length`, `max_parallel_iterations`) and the `--output-dir` flag. They keep working through the 0.10 series with a config-time deprecation warning and will be removed in 0.11; when both spellings are set, the `httpchain_`-prefixed one wins.

### Changed

- **BREAKING**: relative file paths in scenario fields — `body.binary`, `body.files` values, `verify.body.schema`, and `ssl.cert`/`ssl.verify` — now resolve against the **scenario file's directory** (the same rule as `$ref`/`$include`) instead of the pytest invocation directory. Data files can live next to the tests that use them, independent of where pytest runs from; suites that relied on CWD-relative paths must adjust them (absolute paths are unaffected). `validate --deep` file checks follow the same rule.
- **BREAKING**: a duplicate key in a JSON object now fails loading with `Duplicate key '...'` instead of silently keeping the last value. Plain JSON parsing made a duplicated organizational key — e.g. two `"check"` entries in dict-form `response` steps — silently delete the first one, weakening the test with no diagnostic.
- **BREAKING**: a user-function reference without a module path (`"auth": "myfunc"`) now fails at validation/collection with `Module path is required: use 'module:myfunc'` instead of validating and then failing at runtime import. Bare names never worked at runtime; note that a scenario carrying a bare name only in stages that never executed (e.g. permanently skip-marked) previously collected fine and now fails collection — like any other statically-detectable authoring error under strict validation.
- An unhandled model-union variant at any dispatch site (body types, save/verify steps, substitutions, parallel/parametrize steps) now raises loudly instead of being silently skipped — an "unknown body type sends an empty request" class of plugin bug can no longer pass unnoticed.
- **BREAKING**: pytest collection is now free of runtime side effects. Scenario-level `substitutions` resolution, the `auth` user-function call, `ssl` resolution, and httpx client construction are deferred from collection time to the moment a scenario's first stage executes. `pytest --collect-only` and IDE test discovery therefore no longer execute user code (e.g. live token fetches in `auth`) or allocate HTTP clients. Initialization runs at most once per scenario — side-effectful user functions are never re-invoked. A failure in it — an unresolvable scenario template, a raising `auth` function, a bad certificate path — surfaces as a clean `Failed to initialize scenario: ...` failure of the first executed stage, and every later stage (including `always_run` stages, and regardless of `xfail` marks) is skipped with the root cause as the reason, mirroring the previous behavior where a collection error meant no stage ran at all. Exception: when a stage's `parametrize` **values** contain `{{ }}` templates, scenario substitutions still resolve at collection, because pytest needs concrete parameter values to generate test items (templates in parametrize `ids` do not trigger this — they are never substituted). An initialization failure on an `xfail`-marked stage is reported as a real failure — the mark cannot absorb scenario-level breakage into a green run.

## [0.9.1] - 2026-07-14

### Added

- pytest-xdist support policy. Scenarios run in parallel correctly under the class-preserving distribution modes: `--dist loadscope`, `--dist loadfile`, and `--dist loadgroup` (every scenario class now gets an automatic `xdist_group` marker). Modes that distribute tests individually and would silently scatter a multi-stage scenario's stage chain across workers — `--dist load` (the `-n` default), `--dist each`, `--dist worksteal` — now fail collection with guidance instead. Single-stage scenarios have no chain and keep working under every mode.

## [0.9.0] - 2026-07-14

### Added

- Distribution now ships a `py.typed` marker and `[project.urls]` metadata.
- Import layering across the whole package is enforced in CI by an exhaustive import-linter layers contract: every top-level module belongs to exactly one layer, and a new module fails the check until placed.

### Changed

- **BREAKING**: Consolidated the six-distribution uv workspace into the single `pytest-httpchain` distribution. The former sub-packages are now subpackages/modules: `pytest_httpchain.models`, `pytest_httpchain.templates`, `pytest_httpchain.jsonref`, `pytest_httpchain.userfunc`; the shared exception base lives in `pytest_httpchain.errors`. Migration: install `pytest-httpchain>=0.9` only (drop any explicit `pytest-httpchain-*` requirements) and rename imports `pytest_httpchain_X` → `pytest_httpchain.X`. Scenario JSON files, user-function references, ini options, and CLI usage are unaffected.

## [0.8.0] - 2026-06-15

### Added

- New validation diagnostic `HTTPCHAIN019` (error): a scenario- or stage-level `marks` entry that is not a parseable pytest marker (e.g. `skip(` or an unsupported form like `foo.bar`). Markers were previously only parsed at collection time, so `validate` reported such a scenario as OK while `pytest` then aborted collection — `validate` now catches it up front, restoring it as a faithful pre-flight check.
- New validation diagnostic `HTTPCHAIN018` (warning): a `verify.expressions` entry that is a plain string with no `{{ }}` template. Such a value is always truthy at runtime, so the assertion silently passes — the validator now flags the likely forgotten braces.

### Fixed

- A malformed `request.body` shape (an unknown body-type key like `{"jsonn": …}`, an empty `{}`, or a non-object) — and the equivalent for the `save`, `substitution`, response-step, `parametrize`, and `parallel` discriminated unions — now raises a clean, located Pydantic `ValidationError` instead of a bare `ValueError`. The bare error was not a `ValidationError`, so it escaped every `except ValidationError` handler: `validate` aborted with a raw traceback (and emitted a crash dump on `--format json`), `pytest --collect-only` reported the error without naming the offending key, and `show`/`graph` dumped a traceback. All four now produce a coded diagnostic / clean message that names the bad key and lists the valid tags.
- A single-path SSL client certificate (`ssl.cert: "/path/to/client.pem"`) no longer crashes collection. The path was stored as a `pathlib.Path` and handed straight to httpx, which unpacks a non-tuple cert via `load_cert_chain(*cert)` and raised `TypeError`. Cert paths are now stringified for httpx, fixing both the single-path and `[cert, key]` tuple forms.
- A template inside an `ssl.cert` tuple (e.g. `["/certs/{{ name }}", "/certs/key.pem"]`) is now rendered. The template walker had no `tuple` case (and `cert` is the only tuple-typed field), so the `{{ }}` was passed to httpx verbatim. Tuples are now walked like lists.
- An empty `parallel.foreach` (`[]`) or empty `combinations` (`[]`) is now rejected at validation/collection time (`min_length=1`) instead of silently running the request once unparameterized (`foreach`) or failing with a runtime "produced zero iterations" error (`combinations`).
- A non-integer value for a numeric ini option (`ref_parent_traversal_depth`, `max_comprehension_length`, `max_parallel_iterations`) now produces a clean `pytest.UsageError` instead of an `INTERNALERROR` traceback. pytest's `type="int"` handling does a bare `int(value)` that raises `ValueError` before the plugin's range checks run; the reads are now wrapped to report a usage error.
- A plain JSON syntax error in a scenario file is now reported as `HTTPCHAIN014` ("Invalid JSON syntax") instead of the misleading `HTTPCHAIN012` ("JSON reference resolution error"), since no reference is involved. `HTTPCHAIN014` was previously unreachable because the resolver wraps syntax errors as `ReferenceResolverError`; the wrapped cause is now unwrapped to report the accurate code.
- `HTTPCHAIN017` is now listed in the validator module's diagnostic-code table (it was a live, firing code missing from the "full" table).
- The published editor JSON Schema now constrains the string branch of template-accepting fields, so an editor flags a non-template string that is also not a valid value for the field's concrete type — e.g. `timeout: "abc"`, `status: "not-a-status"`, `method: "FOOBAR"`. Templates, concrete values, and the stringified concretes the runtime coerces (`"30"`, `"200"`) remain valid, so no valid scenario is rejected. Runtime validation is unchanged.

### Changed

- PyPI keywords updated: dropped the stale `requests` (the project migrated to httpx in 0.2.0), added `httpx`, `http`, `api`, `integration-testing`.

### Removed

- The `--output`/`-o` option on the `schema` and `resolve` CLI commands. Both commands already default to stdout, so the option (and its `Wrote … to PATH` confirmation) only duplicated shell redirection. Redirect instead — `pytest-httpchain schema > scenario.schema.json` — which makes every command uniformly emit data to stdout.

### Documentation

- Corrected several copy-pasteable examples that failed at runtime: `{{ created_at is not none }}` → `is not None` (responses); subscript access on scenario `vars` (`{{ user['id'] }}`) → attribute access (`{{ user.id }}`) in the parametrization and comprehension examples, with a note that `vars` values are namespaces while fixture/`combinations` dicts use subscript.
- Documented that `verify.headers` are matched by exact, full-string equality (so `Content-Type: application/json` will not match `application/json; charset=utf-8`).
- Documented that the `usefixtures(...)` marker only triggers a fixture's setup/teardown and does **not** inject its value into the template context (use the `fixtures` array for that).
- Documented that `functions` substitutions seed a **callable** that must be invoked with `()` in templates, with usage examples.
- README: corrected the `$ref` paths note (absolute paths are rejected for security) and added a section documenting HAR export via `--output-dir`.
- Documented that `$merge` sibling keys are merged additively (they add keys; overriding an existing scalar is a conflict, not a silent override).
- Documented that a callable fixture value is wrapped as a factory and loses attribute access on the original object.
- Corrected the `max_parallel_iterations` cap docs: an over-cap stage fails at runtime, not at collection.

## [0.7.0] - 2026-06-14

### Added

- `--root-path` option on the `validate`, `resolve`, `show` and `graph` CLI commands to override the directory that constrains `$ref` resolution. The CLI defaults to the nearest `tests/` ancestor while pytest collection uses the repo root, so a `$ref` that resolves during collection can now be made to resolve the same way from the CLI.
- New validation diagnostic `HTTPCHAIN017` (error): a scenario-level `substitutions`/`auth`/`ssl` template references a name that is not a scenario-level substitution. Such references resolve at collection time against only the scenario substitutions, so anything else is a guaranteed collection-time crash — now caught up front with a coded, located diagnostic.

### Fixed

- A failing stage that does not use `parallel` is no longer reported as `Parallel execution failed at iteration 0`. Both the sequential and parallel execution paths share one error mechanism, and the wrapper that labels a failure with its iteration index was applied unconditionally — so an ordinary verify or request failure surfaced with a misleading parallel prefix and a meaningless `iteration 0`. The prefix is now added only when the stage actually configures `parallel`; otherwise the original error (e.g. `Status code doesn't match: expected 200, got 404`) is reported as-is.

- Request rate limiting (`parallel.calls_per_sec`) works again. The limiter was written against the pyrate-limiter 3.x API — it passed a `max_delay=` argument to the `Limiter` constructor and expected delay/bucket-full exceptions — but the project resolves pyrate-limiter 4.x, where that constructor argument was removed. As a result any stage that set `calls_per_sec` crashed with a raw `TypeError` at limiter construction, before a single request was sent, and the feature had no integration coverage to catch it. The limiter is now built with the 4.x API: each request waits up to `max_rate_limit_delay` seconds (default 60) for a slot and fails with a clean `Rate limit exceeded` error if none becomes available within that window. The `pyrate-limiter` dependency floor is raised to `>=4.0.0` to match the API the code now uses.

- A binary (`body.binary`) or multipart-file (`body.files`) request body that points at an unreadable path now fails with a clean error instead of a raw traceback. The body readers caught only `FileNotFoundError`, so sibling `OSError`s — a path that is actually a directory (`IsADirectoryError`), a permission denial (`PermissionError`) — escaped uncaught and surfaced as an internal pytest error that bypassed the normal abort/`xfail` flow. Both readers now catch `OSError` and report it as a `RequestError`; the existing missing-file messages are unchanged.

- Template error messages now show the expression in its real `{{ … }}` form and include the underlying cause. The message was built with an f-string that collapsed `{{ }}` to single braces, so a failing expression was reported as `'{ missing_var }'` — text that never appears in the user's scenario — and the specific simpleeval detail (which name/attribute) was dropped. Messages now render `'{{ missing_var }}'` and append the cause, e.g. `…: 'missing_var' is not defined`.

- A user module whose top-level code raises a non-`ImportError` (for example a `RuntimeError` while importing) is now reported as a clean `UserFunctionError` instead of escaping as a raw traceback. `import_function` caught only `ImportError`; it now wraps any exception raised during import, with the cause preserved in the message.

- A non-string reference value (e.g. `{"$ref": 42}`) now raises a `ReferenceResolverError` instead of a raw `TypeError`, so the plugin reports a clean collection error and the CLI exits non-zero with a message instead of crashing.

- The `schema`/`resolve` CLI commands now report a clean `error: cannot write …` and exit non-zero when `--output` points at an unwritable path (e.g. a missing directory), instead of surfacing a raw traceback.

- A failure while building a test class at collection time — resolving scenario-level substitutions/`ssl`, calling the scenario `auth` function, or constructing the HTTP client — is now reported as a clean collection error instead of a raw internal traceback, matching the load/validate paths.

- `show`/`graph` now attribute a re-saved variable to its most recent producer before the consumer, not the first one, matching the runtime `ChainMap` layering where a later save shadows an earlier one.

- The order-aware validator now models that stage `substitutions` and the `parallel` config resolve *before* any `foreach` iteration variable exists, so a substitution that references a `foreach` parameter is flagged (it fails at runtime) instead of being treated as valid.

- The `HTTPCHAIN002` fixture/variable-conflict check is now scoped per stage. A fixture used only in one stage and a same-named parametrize parameter used only in another stage never coexist and are no longer falsely reported as a collection error.

- Exported HAR files now report the real request duration. The HAR writer's timing parameters were never supplied at the call site, so every entry recorded `time: 0`; the duration is now taken from the response's elapsed time.

- HTTP response report sections no longer dump unbounded binary mojibake. A response with a non-textual content type is summarised as `<binary N bytes>`, an unreachable decode-error branch was removed, and the response body is truncated with the same limit as the request body.

- An exception raised by a user function or factory fixture invoked *inside* a `{{ }}` expression is now wrapped as a `TemplatesError` (with the cause in the message) instead of escaping as a raw traceback, honoring the documented all-errors-are-`TemplatesError` contract.

- Request report sections no longer mislabel a text body that merely fails JSON parsing as `<Binary content>`. The body is decoded first; only genuinely undecodable bytes get the binary placeholder, while text that fails to parse as JSON is shown as-is.

- Importing `pytest-httpchain-models` no longer mutates the process-wide Python warnings filters as an import side effect. The suppression of Pydantic's field-shadow warning (for the `json`/`schema` body fields) is now scoped with `warnings.catch_warnings()` to only the two model classes that need it.

### Changed

- `parallel.foreach` with an `individual` step now rejects a multi-parameter dict. Only one parameter per `individual` step was ever honored — extra keys were silently discarded — so a dict with more than one key now fails validation (with the offending location) instead of quietly dropping parameters.

- The main `pytest-httpchain` package now declares the dependencies it imports directly (`pytest`, `jmespath`, `jsonschema`, `simpleeval`, and the `pytest-httpchain-core`/`pytest-httpchain-userfunc` workspace packages), which were previously only resolved transitively through sibling packages.

- A failing parallel stage now commits no saved variables. Previously the saves collected from whichever iterations happened to finish before the error were committed to the global context, so which values survived a parallel failure depended on thread timing; a failed stage now leaves the global context unchanged (deterministic).

- A malformed marker on a *stage* now fails collection with a clear error instead of being silently dropped while the stage still runs — matching how an invalid scenario-level marker is already handled.

- The numeric ini options (`ref_parent_traversal_depth`, `max_comprehension_length`, `max_parallel_iterations`) are registered as integers, so a non-integer value is rejected by pytest with a clean message and an out-of-range value raises a pytest usage error instead of an `INTERNALERROR` traceback. The `pytest` dependency floor is raised to `>=8.4` (required for integer ini options).

- Context-variable names — `vars` keys, function-substitution aliases, and JMESPath save keys — must now be valid Python identifiers. A non-identifier key (e.g. `my-var`) could never be referenced in a `{{ }}` expression and is now rejected at validation instead of silently producing an unusable variable.

- The function-reference format (`module.path:function`) is validated more strictly: a malformed module path (e.g. `a..b:f`, `mod.:f`) is rejected at validation/collection time instead of failing later at import.

- `$ref` resolution no longer falls back to the current working directory. References resolve purely from the referencing file's directory and the configured root, so resolution no longer depends on where pytest was launched.

- An object that contains more than one reference directive (any two of `$ref`/`$include`/`$merge`) now raises an error instead of silently honoring one and discarding the rest.

- `wrap_function` in the `pytest-httpchain-userfunc` package dropped its unused `default_args` parameter; pass positional arguments at call time instead. `default_kwargs` is unchanged.

- The four workspace sub-packages (`pytest-httpchain-jsonref`, `-models`, `-templates`, `-userfunc`) now ship real READMEs as their PyPI landing pages, where previously each shipped an empty file.

### Security

- Absolute `$ref`/`$include`/`$merge` paths are now rejected. An absolute path bypassed the parent-traversal limit (it contains no `..`), so a scenario could reference a file anywhere on disk (verified by reading `/etc/hostname`); the resolver now rejects absolute reference paths outright.

## [0.6.0] - 2026-06-13

### Added

- New validation diagnostic `HTTPCHAIN009` (warning): a stage saves a variable whose name is also a scenario-level fixture. The fixture value takes precedence in every stage, so such a save can never be read back.
- New validation diagnostic `HTTPCHAIN016` (error): a fixture is referenced in a scenario-level template (`substitutions`, `auth`, or `ssl`). Those templates resolve once at collection time, before any fixture exists, so such a reference is a guaranteed collection-time crash — now caught by `validate` and collection-time validation instead.
- The order-aware data-flow validator now checks `always_run` template references against their actual evaluation scope — fixtures, parametrize parameters, scenario substitutions, and earlier saves (`HTTPCHAIN003`/`HTTPCHAIN004`) — and `show`/`graph` count them as variable consumption.

### Changed

- **BREAKING**: scenario models now reject unknown keys (every model derives from a shared `extra="forbid"` base). A misspelled field — `"headerz"`, `"alwaysrun"`, `"statu"` — fails validation at collection time with the offending key and its location, instead of being silently ignored and producing a wrong request. The documented `"$schema"` editor key keeps working: models discard it during validation, whether it sits at the top of the test file or at the root of a fragment pulled in by `$include`/`$merge`/`$ref`. A `"$schema"` inside plain data (an inline response-body JSON Schema, a JSON body) is preserved. Migration note: an undocumented pattern of stashing reusable nodes under a custom top-level key (e.g. `"definitions"`) for same-document `#/...` pointers is now rejected — move the stash to a separate fragment file and reference it with `file.json#/...` pointers.

### Fixed

- A whole-string template padded with surrounding whitespace now preserves its type. A value that is a single `{{ … }}` expression with leading or trailing whitespace — `" {{ a == b }} "` — was accepted by schema validation as a complete (type-preserving) template but evaluated at runtime as string interpolation, yielding a *string* instead of the typed value. For `verify.expressions` this was a silent false-negative: the result `" False "` is a non-empty, truthy string, so the assertion passed even when the expression was false; for `always_run` the stage always ran; for `repeat`/`timeout`/`max_concurrency`/etc. it produced a string where a number was expected. Runtime single-expression detection now uses the same whitespace-tolerant predicate as the models, so `" {{ a == b }} "` evaluates to the bool `False`. Note: the surrounding whitespace (spaces, tabs, newlines) is stripped from such whole-string templates — a value that previously carried a leading/trailing newline into its output via interpolation now returns the bare typed value.

- User-function error messages now include the underlying cause. When an `auth`, `save`, or `verify` function failed to import or raised at call time, the error read only `Error calling function '<name>'` / `Failed to import module '<path>'` — the real exception (a `KeyError`, a connection failure, the actual `ImportError`) was attached as `__cause__` but never shown, because stage failures are reported with `pytest.fail(..., pytrace=False)` and the validator embeds only the message text. The two wrappers now append `: {cause}` (matching the wrapper already used for `functions` substitutions), so the actual reason reaches the test output and validation diagnostics. In particular, a module that is missing now reads differently from one that exists but fails to import.

- Circular-reference detection no longer raises a phantom cycle when two documents reuse the same internal JSON pointer. Internal pointers (`#/a`) were tracked by pointer string only and inherited into the tracker used for external files, so a document referencing `#/a` whose subtree pulled in another file that referenced *its own* `#/a` failed to load with `Circular reference detected: #/a`. Internal pointers are document-local and are no longer carried across a file boundary; genuine internal cycles (within one document) and cross-document cycles (tracked by file + pointer) are still detected.

- The published editor JSON Schema now actually validates scenario files. Its `JsonRef` wrapper accepted *any* object (no required keys), so editors caught neither typos nor missing required fields anywhere an object was expected. A reference object must now carry one of `$ref`/`$include`/`$merge`; combined with unknown-key rejection above, editors flag misspelled fields as-you-type. Tagged unions are emitted as `anyOf` instead of `oneOf`, so a reference object at a union position (a `save` value, a request `body`, a `parallel` config…) is no longer rejected as ambiguous. The schema root also explicitly declares `$schema` and the three reference directives.

- `always_run` template expressions are now actually evaluated. Previously the runtime tested the raw field for truthiness, so any template string — e.g. `"always_run": "{{ should_run }}"` — behaved as `always_run: true` regardless of what it evaluated to. The template is now resolved (with Python truthiness) when an earlier stage has failed, against fixtures, parametrize parameters, scenario substitutions, and previously saved variables; a template that fails to evaluate fails the stage with a clear message instead of silently running it.

- Restored scenario-level `fixtures`: the documented top-level `fixtures` field (pytest fixtures available to all stages) had been silently dropped from the `Scenario` model in an earlier refactor — scenarios using it passed validation but failed at runtime with undefined-variable errors. The field is back in the model and the generated JSON Schema, fixtures are injected into every stage (deduplicated against stage-level `fixtures`), and `show`/`graph` report them from the model.
- The validator and `show`/`graph` no longer treat an undocumented top-level `vars` key as a variable source. The runtime never read it; with unknown keys now rejected, such a file fails validation outright instead of validating "OK" and failing at runtime. Scenario-level variables belong in `substitutions`.

## [0.5.0] - 2026-06-04

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

## [0.2.4] - 2026-04-02

### Added

- HAR export: the `--output-dir` pytest option writes each test's HTTP request/response exchange to a HAR file for inspection and debugging.
- A full MkDocs documentation site (`docs/`), replacing the single `USAGE.md` guide.
- A generated JSON Schema for scenario files, enabling editor autocomplete and validation.
- An `install` command and a bundled MCP server for AI code-assistant integration (the Claude Code skill plus optional MCP server config).
- New integration tests covering request/save/schema error paths (connection refused, invalid hostname, malformed JSON in save and schema verification).

### Changed

- User-function imports now also search relative paths, so functions can be referenced from modules alongside the scenario file.
- pytest markers declared in scenarios are now parsed with `ast.literal_eval` instead of the template/`simpleeval` engine, so marker arguments are interpreted as plain Python literals.

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

[Unreleased]: https://github.com/aeresov/pytest-httpchain/compare/v0.10.0...HEAD
[0.10.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/aeresov/pytest-httpchain/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.8.1...v0.9.0
[0.8.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.2.4...v0.3.0
[0.2.4]: https://github.com/aeresov/pytest-httpchain/compare/v0.2.1...v0.2.4
[0.2.1]: https://github.com/aeresov/pytest-httpchain/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/aeresov/pytest-httpchain/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/aeresov/pytest-httpchain/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/aeresov/pytest-httpchain/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/aeresov/pytest-httpchain/releases/tag/v0.1.0