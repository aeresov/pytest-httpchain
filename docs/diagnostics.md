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
| `HTTPCHAIN004` | warning | Variable referenced before it is saved or defined — saved by a later stage, or defined by a later substitution step (ordering / data-flow) |
| `HTTPCHAIN005` | warning | Stage has no verify step (no response validation) |
| `HTTPCHAIN006` | warning | Verify step asserts nothing (no-op) |
| `HTTPCHAIN007` | error | Body `contains`/`not_contains` list the same substring |
| `HTTPCHAIN008` | error | Body `matches`/`not_matches` list the same pattern |
| `HTTPCHAIN009` | warning | Saved variable is shadowed by a scenario-level fixture |
| `HTTPCHAIN010` | error | File not found |
| `HTTPCHAIN011` | error | Path is not a file |
| `HTTPCHAIN012` | error | `$ref` resolution failed |
| `HTTPCHAIN013` | warning | File extension is not `.json` |
| `HTTPCHAIN014` | error | Invalid JSON syntax |
| `HTTPCHAIN015` | error | Failed to parse JSON file |
| `HTTPCHAIN016` | error | Fixture referenced in a scenario-level template |
| `HTTPCHAIN017` | error | Scenario-level template references an undefined name |
| `HTTPCHAIN018` | warning | Verify expression is not a template (`{{ }}`) — asserts nothing |
| `HTTPCHAIN019` | error | Invalid pytest marker expression (scenario or stage `marks`) |
| `HTTPCHAIN020` | warning | Referenced file does not exist (deep, opt-in) |
| `HTTPCHAIN021` | warning | Schema file is not valid JSON / not a valid schema (deep) |
| `HTTPCHAIN022` | warning | User function cannot be imported (deep) |
| `HTTPCHAIN023` | warning | Unexpected argument passed to a user function (deep) |
| `HTTPCHAIN024` | warning | Missing required argument for a user function (deep) |
| `HTTPCHAIN025` | info | Template parametrize values force collection-time resolution |
| `HTTPCHAIN026` | warning | `$ref` path matches files under both lookup bases (ambiguous) |
| `HTTPCHAIN027` | warning | User-defined name shadowed by the reserved `response` namespace |
| `HTTPCHAIN028` | warning | Scenario directive (`$include`/`$merge`, or file-path `$ref`) inside an inline JSON Schema — not resolved there |

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
