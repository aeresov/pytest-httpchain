# Validation diagnostics

Every finding from the scenario validator carries a **stable diagnostic code**
(`HTTPCHAINxxx`), a severity, a human-readable message and (where meaningful) a
location, so tooling can filter, sort and route diagnostics deterministically.

You meet these codes in three places:

- `pytest-httpchain validate` output (and its `--format json` payload);
- **pytest collection**: error-severity findings fail collection, warnings are
  emitted as `ScenarioValidationWarning` in the form `[HTTPCHAINxxx] ...`;
- CI gates: `validate --strict` exits non-zero on warnings too.

Codes are append-only: a code's meaning never changes, and retired checks do
not free their numbers for reuse.

## Code reference

| Code | Severity | Meaning |
| --- | --- | --- |
| `HTTPCHAIN000` | error | Schema validation failed (Pydantic `Scenario` model) |
| `HTTPCHAIN001` | error | Duplicate stage names |
| `HTTPCHAIN002` | error | Fixture and variable share the same name |
| `HTTPCHAIN003` | warning | Variable referenced but never defined/saved/fixture (typo) |
| `HTTPCHAIN004` | warning | Variable referenced before it is saved or defined — saved by a later stage or by a later step of the same stage's response, or defined by a later substitution step (ordering / data-flow) |
| `HTTPCHAIN005` | warning | Stage has no verify step (no response validation) |
| `HTTPCHAIN006` | warning | Verify step asserts nothing (no-op) |
| `HTTPCHAIN007` | error | Body or header matcher `contains`/`not_contains` list the same substring, or a `verify.jmespath` matcher's `contains` and `not_contains` are the same JSON value |
| `HTTPCHAIN008` | error | Body, header or `verify.jmespath` matcher `matches`/`not_matches` list the same pattern |
| `HTTPCHAIN009` | warning | Saved variable is shadowed by a scenario-level fixture |
| `HTTPCHAIN010` | error | File not found |
| `HTTPCHAIN011` | error | Path is not a file |
| `HTTPCHAIN012` | error | `$ref` resolution failed |
| `HTTPCHAIN013` | warning | File extension is not `.json` |
| `HTTPCHAIN014` | error | Invalid JSON in the scenario or a file it includes: a syntax error, a duplicate object key, bytes that are not UTF-8, or an integer too long to parse |
| `HTTPCHAIN015` | error | Failed to parse JSON file (for example, nested too deeply to parse) |
| `HTTPCHAIN016` | error | Fixture referenced in a scenario-level template |
| `HTTPCHAIN017` | error | Scenario-level template references an undefined name |
| `HTTPCHAIN018` | warning | Verify expression is not a template (`{{ }}`) — cannot evaluate to the required bool |
| `HTTPCHAIN019` | error | Invalid pytest marker expression (scenario or stage `marks`) |
| `HTTPCHAIN020` | warning | Referenced file does not exist (deep, opt-in): a file path, a body schema file, or a local file a body schema's `$ref` or `$dynamicRef` names |
| `HTTPCHAIN021` | warning | A body schema cannot be used (deep): its file is not JSON, its JSON pointer leads nowhere, an `$id` on the pointer's way or in the schema cannot be read (not a string, or not a URI), the schema it selects is not valid, or a `$ref` or `$dynamicRef` it reaches does not resolve (not a string, remote, an absolute path, more `../` than `httpchain_ref_parent_traversal_depth`, outside the root, a pointer to nothing, a malformed `$id`) or points to an invalid schema |
| `HTTPCHAIN022` | warning | User function cannot be imported (deep) |
| `HTTPCHAIN023` | warning | Unexpected argument passed to a user function (deep) |
| `HTTPCHAIN024` | warning | Missing required argument for a user function (deep) |
| `HTTPCHAIN025` | info | Template parametrize values force collection-time resolution |
| `HTTPCHAIN026` | warning | `$ref` path matches files under both lookup bases (ambiguous) |
| `HTTPCHAIN027` | warning | User-defined name shadowed by the reserved `response` namespace |
| `HTTPCHAIN028` | warning | Scenario directive (`$include`/`$merge`) inside an inline JSON Schema — not resolved there; a JSON Schema `$ref` is, to a local file too |
| `HTTPCHAIN029` | warning | Template expression in a dict **key** — only values are substituted, so the key is sent literally (a `verify.jmespath` key is evaluated as written; one that is not valid JMESPath fails validation instead) |
| `HTTPCHAIN030` | warning | Template expression in a `functions` substitution's **kwargs** — kwargs are passed to the function unrendered, so it arrives as literal text |
| `HTTPCHAIN031` | error | `xdist_group` marker in a **stage's** `marks`, naming a group the scenario does not declare — under `--dist loadgroup` it runs that stage apart from the rest of the scenario; put it in the scenario's `marks` (see [pytest-xdist](advanced/parallel.md#running-scenarios-in-parallel-with-pytest-xdist)) |
| `HTTPCHAIN032` | error | Stage name contains `::`, pytest's node-id separator — the stage cannot be run by its node id, and `--dist loadscope` runs it apart from the rest of the scenario |
| `HTTPCHAIN033` | error | Scenario's `xdist_group` name has a `]` after its last `@` — pytest-xdist ignores such a group, so `--dist loadgroup` does not keep the scenario's stages together |
| `HTTPCHAIN034` | error | Stage `request.url` is relative (`/users/1`, or `/users/{{ id }}`), but the scenario's `client` sets no `base_url` to resolve it against (see [Client configuration](usage/scenarios.md#client-configuration)) |
| `HTTPCHAIN035` | warning | A [built-in function](usage/substitutions.md#built-in-functions) that is no use as a value (a time, encoding, URL or hashing helper, `uuid4`, `env`, `rand` or `randint`) used without calling it (`{{ now }}` for `{{ now() }}`, `{{ env }}`, or `str(timestamp)`) — the template gets the function itself, not its value, and one that renders to a function fails the stage (a parametrize value, collection; a scenario-level template, scenario initialization) |
| `HTTPCHAIN036` | warning | A [built-in's](usage/substitutions.md#your-names-come-first) name the scenario defines too, used as a function where that definition is not in scope (in another stage, in a scenario-level template, in a step before the one defining it): called (`timestamp()`) where the scenario's is a fixture or function substitution, or handed to a function (`sorted(rows, key=len)`) — the built-in runs in its place, silently |

## Deep (opt-in) checks

`HTTPCHAIN020`–`HTTPCHAIN024` come from *deep* validation
(`validate --deep`), which imports your user modules and touches the
filesystem — so it is opt-in and never runs at pytest collection time. Deep
findings are always warnings; pair `--deep` with `--strict` to fail CI on
them.

## Filtering collection warnings

At collection time, **warning**-severity findings are emitted as
`pytest_httpchain.ScenarioValidationWarning` (error-severity findings fail
collection outright, and **info** findings — `HTTPCHAIN025` — never affect
validity, are exempt from `--strict`, and are not warned about at collection).
Standard warning filters apply — e.g. to silence one code project-wide:

```ini
# pytest.ini
[pytest]
filterwarnings =
    ignore:.*HTTPCHAIN005.*:pytest_httpchain.ScenarioValidationWarning
```
