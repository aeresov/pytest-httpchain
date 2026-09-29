# Command line

Installing `pytest-httpchain` also installs a `pytest-httpchain` command. None
of it runs your tests or makes an HTTP request — the commands read scenario
files and report on them. The one exception to "reads only" is
`validate --deep`, which imports the modules your `module:func` references name,
and therefore executes their top-level code; that is why it is opt-in and never
runs at pytest collection time. Every command reads a file as collection does,
[comments and trailing commas](usage/scenarios.md#comments-and-trailing-commas)
included, whether it is named `.json` or `.jsonc`.

If you only want to try one, `uvx pytest-httpchain --help` needs no install:

```bash
uvx pytest-httpchain --help
uvx pytest-httpchain --version
```

## Common options

Two options appear on every command that reads a scenario file, because both
affect how `$include`/`$merge`/`$ref` resolve:

| Option | Default | Meaning |
| --- | --- | --- |
| `--root-path DIR` | auto-detected | Directory references must not escape. Collection uses pytest's `rootdir`; the CLI approximates it, so pass this explicitly when the two disagree. |
| `--ref-parent-traversal-depth N` | `3` | How many `../` levels a reference may climb. Mirrors the `httpchain_ref_parent_traversal_depth` ini option. |

The auto-detected root prefers a directory holding real pytest configuration
(`pytest.ini`, or a `pyproject.toml` with a `[tool.pytest…]` section) over a bare
project marker — so a sub-package's own `pyproject.toml` does not shrink the
root below pytest's and make `validate` reject references that collection
resolves fine.

## `validate`

Check scenario files and exit non-zero if any is invalid. This is the CI gate.
Name the files, or the directories that hold them:

```bash
pytest-httpchain validate tests/
pytest-httpchain validate tests/test_login.http.json tests/test_orders.http.jsonc
```

A file you name is validated whatever its name. One named anything but `.json`
or `.jsonc` gets an `HTTPCHAIN013` warning: pytest would not collect it.

Findings carry a stable `HTTPCHAINxxx` code and a severity — see
[Validation diagnostics](diagnostics.md) for the full table.

| Option | Meaning |
| --- | --- |
| `--format text\|json` | `json` emits the whole result, including each diagnostic's `code`, `severity`, `message` and `location`, for editor and CI integration. |
| `--strict` | Treat warnings as failures for the exit code. |
| `--suffix SUFFIX` | Search directories for `test_<name>.<SUFFIX>.json` and `.jsonc` files. Default: the `httpchain_suffix` your pytest configuration sets, else `http` (see [Directories](#directories)). |
| `--deep` | Also import your `module:func` references and check their signatures, and confirm referenced files and schema files exist. A body schema is followed as the runtime follows it: its JSON pointer must resolve, the schema it selects must be valid, and every `$ref` and `$dynamicRef` it reaches must resolve to a valid schema, locally and under the path rules a scenario's `$include` keeps (`--root-path`, `--ref-parent-traversal-depth`) (see [JSON Schema validation](usage/responses.md#a-schema-inside-a-document-openapi-and-shared-schema-files)). |
| `--syspath DIR` | Extra directory on `sys.path` for `--deep` import resolution. Repeatable. |

`--deep` imports your code, which is why it is opt-in and never runs at pytest
collection time:

```bash
pytest-httpchain validate --deep --strict --syspath tests tests/
```

In the JSON payload, the top-level `valid` is the gate result — it matches the
exit code and accounts for `--strict` — while each file's own `result.valid` is
pure validity. The sibling `strict` key tells the two apart. `files` holds an
entry per path reported, in the text report's order: each file checked, and
each directory without scenario files, with its `HTTPCHAIN039`.

The same semantic checks run at pytest collection time, so `pytest
--collect-only` validates your whole suite: error-severity findings fail
collection, warnings become `ScenarioValidationWarning`. A file that fails to
load reports the identical diagnostic code either way.

### Directories

A directory is searched the way pytest collects it, so `validate tests/` checks
the files `pytest tests/` runs:

- every file named `test_<name>.<suffix>.json` or `test_<name>.<suffix>.jsonc`,
  at any depth;
- except in the directories pytest skips by default: those its default
  `norecursedirs` matches (`*.egg`, `.*`, `_darcs`, `build`, `CVS`, `dist`,
  `node_modules`, `venv`, `{arch}`), `__pycache__`, and virtual environments (a
  directory holding a `pyvenv.cfg`, or a `conda-meta/history` for a conda
  environment). A directory you name is searched whatever its name. Your
  project's own `norecursedirs`, `--ignore` and `collect_ignore` are not read;
- passing over an entry pytest passes over because it cannot be looked at, such
  as a symlink to itself or a file deleted during the search; any other such
  failure (a permission error) stops `validate` with an `error:` line, as it
  stops pytest;
- not entering a symlink to a directory (or a Windows junction), so a link
  cannot send the search round in a loop. This is where `validate` differs from
  pytest, which follows one. A symlink to a file is checked like the file, and a
  directory you name may be a symlink.

The suffix is the `httpchain_suffix` set in the configuration file pytest would
read for the same paths (`pytest.toml`, `pytest.ini`, `pyproject.toml`,
`tox.ini` or `setup.cfg`, found as pytest
[finds its configfile](https://docs.pytest.org/en/stable/reference/customize.html#initialization-determining-rootdir-and-configfile)),
else `http`. `--suffix` overrides it, as `-o httpchain_suffix=...` overrides it
for pytest (`validate` reads neither `-o` nor `PYTEST_ADDOPTS`). A configuration
file pytest could not read, or a suffix it would refuse, stops `validate` with
an `error:` line before anything is checked. Files you name need no suffix, so
the configuration is read only when a path is a directory.

The report is sorted by path, whatever order the paths are given in, so it does
not change with the order a shell or `find` lists them in. Paths are compared
name by name, which puts a directory's files in pytest's order: depth first,
each directory's entries by name. A file reached twice, as `validate tests
tests/api` reaches `tests/api`'s, is checked once, under the path it was first
reached by. After more than one file, a line sums the run up, counting a file
with errors under errors only:

```console
$ pytest-httpchain validate tests/
tests/api/test_orders.http.jsonc: OK with warnings
  warning [HTTPCHAIN003]: Stage 'list': request references potentially undefined variable(s): ['order_id'] (at stages[0].request)
tests/api/test_users.http.json: INVALID
  error [HTTPCHAIN001]: Duplicate stage names found: ['get'] (at stages)
tests/test_login.http.json: OK
3 files checked, 1 with errors, 1 with warnings
```

A directory holding no scenario file fails the run with `HTTPCHAIN039`, and a
path that does not exist with `HTTPCHAIN010`, so a mistyped path, or a suffix
that names no file, cannot pass CI as an empty run. Neither is a file checked:
the summary counts them apart, `..., 1 path not found, 1 directory without
scenario files`.

## `show`

Summarize a scenario: its stages, what each one saves, and where each consumed
variable comes from.

```console
$ pytest-httpchain show tests/integration/examples/save/test_save_jmespath.http.json
test_save_jmespath.http.json
2 stage(s) · fixtures: server

1 · save_jmespath    GET {{ server }}/users
    saves:    first_user, first_user_name, user_count
2 · use_saved_values    GET {{ server }}/user/{{ first_user.id }}
    saves:    fetched_name
    consumes: first_user (from #1 save_jmespath), first_user_name (from #1 save_jmespath), user_count (from #1 save_jmespath)
```

The first line is the scenario's `description`, or the file name when it has
none. A stage also lists its `marks:` when it declares any, and its `skip_if:`
when it may [skip](usage/scenarios.md#skipping-a-stage-at-runtime). The chain
goes on past a skipped stage, and a later stage then reads what an earlier
stage saved under the same name, or finds nothing. So a name whose nearest
producer has a `skip_if` lists every stage it may come from, nearest first,
back to one without a `skip_if`: `token (from #2 refresh, else #1 login)`.

`--format json` emits the same data-flow model (stages, edges, scenario
fixtures and vars) for tooling. Each stage carries its `skip_if` as declared,
`false` when it has none.

## `graph`

Render the stage data-flow as a [Mermaid](https://mermaid.js.org) flowchart.
Edges are labelled with the variables that create the dependency. An edge out
of a stage with a `skip_if` is dotted (`-.->`). When that stage skips, the
consumer reads those names from an earlier stage that saved them, so every such
stage has its own edge too, back to the nearest one without a `skip_if`, whose
edge is solid; when none saved them, the consumer finds nothing.

```console
$ pytest-httpchain graph tests/integration/examples/save/test_save_jmespath.http.json
flowchart TD
    S0["1 · save_jmespath"]
    S1["2 · use_saved_values"]
    S0 -->|first_user, first_user_name, user_count| S1
```

`--direction` takes `TD` (top-down, the default) or `LR` (left-to-right). The
output is Mermaid source; paste it into any renderer that accepts it, including
GitHub Markdown.

## `resolve`

Print the scenario with every `$include`/`$merge`/`$ref` inlined and
deep-merged — what collection actually sees. Useful when a merge is not
producing what you expected. The output is strict JSON: the
[comments and trailing commas](usage/scenarios.md#comments-and-trailing-commas)
a scenario or an included file may hold are not in it, so it is also a way to
turn a `.jsonc` scenario into plain JSON.

```bash
pytest-httpchain resolve tests/test_login.http.json
```

Inline JSON Schemas under `verify.body.schema` pass through verbatim, exactly as
they do at collection time: their `$ref` and `$defs` address the schema
validator, not the scenario resolver.

## `schema`

Emit the JSON Schema for scenario files, matching your installed version.

```bash
pytest-httpchain schema > scenario.schema.json
```

Point your editor at it with a `$schema` key in each test file for as-you-type
validation and autocomplete. Published copies are also served per release — see
[Getting Started](getting-started.md).
