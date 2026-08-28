# Command line

Installing `pytest-httpchain` also installs a `pytest-httpchain` command. Every
subcommand is read-only: nothing here runs your tests or makes an HTTP request,
so all of it is safe against a production scenario file.

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

Check one or more scenario files and exit non-zero if any is invalid. This is
the CI gate.

```bash
pytest-httpchain validate tests/test_login.http.json
pytest-httpchain validate tests/**/*.http.json
```

Findings carry a stable `HTTPCHAINxxx` code and a severity — see
[Validation diagnostics](diagnostics.md) for the full table.

| Option | Meaning |
| --- | --- |
| `--format text\|json` | `json` emits the whole result, including each diagnostic's `code`, `severity`, `message` and `location`, for editor and CI integration. |
| `--strict` | Treat warnings as failures for the exit code. |
| `--deep` | Also import your `module:func` references and check their signatures, and confirm referenced files and schema files exist. |
| `--syspath DIR` | Extra directory on `sys.path` for `--deep` import resolution. Repeatable. |

`--deep` imports your code, which is why it is opt-in and never runs at pytest
collection time:

```bash
pytest-httpchain validate --deep --strict --syspath tests tests/test_login.http.json
```

In the JSON payload, the top-level `valid` is the gate result — it matches the
exit code and accounts for `--strict` — while each file's own `result.valid` is
pure validity. The sibling `strict` key tells the two apart.

The same semantic checks run at pytest collection time, so `pytest
--collect-only` validates your whole suite: error-severity findings fail
collection, warnings become `ScenarioValidationWarning`. A file that fails to
load reports the identical diagnostic code either way.

## `show`

Summarize a scenario: its stages, what each one saves, and where each consumed
variable comes from.

```console
$ pytest-httpchain show tests/test_save_jmespath.http.json
test_save_jmespath.http.json
2 stage(s) · fixtures: server

1 · save_jmespath    GET {{ server }}/users
    saves:    first_user, first_user_name, user_count
2 · use_saved_values    GET {{ server }}/user/{{ first_user.id }}
    saves:    fetched_name
    consumes: first_user (from #1 save_jmespath), first_user_name (from #1 save_jmespath)
```

The first line is the scenario's `description`, or the file name when it has
none. A stage also lists its `marks:` when it declares any.

`--format json` emits the same data-flow model (stages, edges, scenario
fixtures and vars) for tooling.

## `graph`

Render the stage data-flow as a [Mermaid](https://mermaid.js.org) flowchart.
Edges are labelled with the variables that create the dependency.

```console
$ pytest-httpchain graph tests/test_save_jmespath.http.json
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
producing what you expected.

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
