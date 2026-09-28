# pytest_httpchain.jsonref

JSON reference resolution engine for pytest-httpchain.

## Purpose

This subpackage provides JSON file loading with reference resolution support. Three directives are supported:

- **`$include`** (preferred): Avoids conflicts with VS Code's JSON Schema validation
- **`$merge`** (preferred): Alias for `$include`, semantically clearer when merging
- **`$ref`** (legacy): Standard JSON Reference syntax, but may cause VS Code validation issues

Features:
- External file references (`"$include": "other.json"`)
- JSON pointer references (`"$include": "#/definitions/foo"`)
- Combined references (`"$include": "other.json#/definitions/foo"`)
- Deep merging of sibling properties with referenced content

## Public API

```python
from pytest_httpchain.jsonref import load_json, json_equal, ReferenceResolverError, InvalidJSONError

# Load JSON with $ref resolution
data = load_json(path, max_parent_traversal_depth=3, root_path=None, opaque=None, atomic=None)

# Equality as JSON means it: True is not 1, 1 is 1.0, containers compared member by member
json_equal([True, {"a": 1}], [True, {"a": 1.0}])  # True
json_equal([True], [1])  # False
```

`json_equal` is the one definition of JSON equality in the plugin: the sibling
merge uses it (an equal value keeps, a different one conflicts), and so do
`verify.jmespath` (`response_steps`) and the validator's contradiction checks
on it. It lives here, the lowest layer that needs it, so all three agree.

### Load errors

Every failure surfaces as `ReferenceResolverError`, so callers need one
`except`. Content the reader rejects — bytes that are not UTF-8 (a UTF-8
byte-order mark is accepted), an integer too long to parse, a duplicate object
key — is an `InvalidJSONError` naming the file, raised directly (`DuplicateKeyError`
is one). Any other load failure is chained as `__cause__`: `OSError`,
`JSONDecodeError`, or `RecursionError` (nested deeper than the decoder or the
resolver's own walk can go). The validator classifies on the type and that
cause. jsonref sits below `utils` in the layering, so it keeps its own list of
these errors (`_LOAD_ERRORS` in `plumbing/reference.py`) instead of importing
the plugin's.

### Opaque subtrees

`opaque` is an optional predicate over document positions (tuples of dict
keys / list indices from the root). A subtree at a matching position passes
through **verbatim** — no directive resolution, no merging — even when it
contains `$ref`/`$include`/`$merge` keys. Positions compose across file
boundaries: content spliced in via a reference is judged at the reference
site's position plus its fragment-relative path. The consumer supplies the
predicate because only it knows which positions hold foreign vocabulary —
pytest-httpchain's load pipeline (`validation.is_inline_schema_position`)
uses it for inline `verify.body.schema` values, where `$ref`/`$defs` belong
to the JSON Schema validator, not this resolver.

Opacity extends to sibling merging: an opaque position merges **atomically**
(equal values keep, differing values raise `Merge conflict`) instead of the
recursive dict merge — two foreign-vocabulary subtrees are never blended.

### Atomic positions

`atomic` is a second position predicate, for merging only: a value at a
matching position merges atomically, as an opaque one does, but its content
is resolved as usual (a `$include` inside it still works). It is for lists
whose entries are alternatives, where concatenation would widen what they
accept, and for values that are one expected value, where concatenating or
blending would assert what neither side wrote: pytest-httpchain passes
`validation.merges_whole`, which matches `verify.status`, each
`verify.jmespath` expectation and each operand of a matcher there. Both
predicates compose across file boundaries the same way.

The merge root itself is exempt from `atomic`: a reference written *at* an
atomic position with siblings beside it (`{"$merge": "common.json#/price",
"lt": 100}`) is one value composed on purpose, not two written for the same
position, so it merges key by key as anywhere else. Only a value that arrives
at the position from both sides of an enclosing reference is kept whole. So
the consumer marks the positions one level down too when they hold one value
each (a matcher's operands): there two values *are* written for the same
position, and a key by key merge of the root must still keep each whole.

## Key Behaviors

### Reference Resolution
All three directives (`$include`, `$merge`, `$ref`) work identically:
- External refs: `{"$include": "file.json"}` loads and merges entire file
- Pointer refs: `{"$include": "#/path/to/node"}` references within same document
- Combined: `{"$include": "file.json#/path"}` references specific node in external file

### Two-Candidate Lookup
A relative reference path is tried against the referencing file's directory first, then against `root_path`; the first existing file wins. When BOTH exist, the file-relative one is used and `AmbiguousReferenceWarning` (from `pytest_httpchain.warnings`) is emitted — the validator surfaces it as `HTTPCHAIN026`.

### Deep Merging
When `$include` (or `$ref`) has sibling properties, they are merged **additively** with the referenced content: sibling keys are added, lists are **concatenated** (except at opaque and atomic positions), and nested dicts are merged recursively. There is **no** last-wins override — a sibling that would override an existing scalar (or conflicts by type) raises `ReferenceResolverError` (`Merge conflict at <path>`) rather than silently winning. Equal is `json_equal`, at any depth, so a position kept whole conflicts on `[true]` against `[1]`. `null` is a value like any other (not an override or a hole): a `null` paired with a different value at the same path is a conflict, while equal values — including two `null`s — merge fine. The whole policy lives in ONE place, `plumbing/reference.py`: `_SIBLING_MERGER` (a custom `deepmerge.Merger`) whose fallback and type-conflict strategies raise through `_raise_on_conflict`, and `_build_atomic_aware_merger`, the same merger with the opaque and atomic positions kept whole by that same function.
```json
{
  "$include": "base.json",
  "extra": "value"  // merged with referenced content
}
```

### File Content
Every file is read as UTF-8, with an optional byte-order mark (`utf-8-sig`). Content the one reader (`_parse_json_rejecting_duplicates`) cannot parse, short of a syntax error, raises `InvalidJSONError` (a `ReferenceResolverError`) naming that file — the referenced one when that is where it failed: bytes that are not UTF-8, a duplicate key (`DuplicateKeyError`, a subclass), an integer past Python's int-string conversion limit. The validator dispatches on `InvalidJSONError` to report `HTTPCHAIN014`, like a syntax error, rather than `HTTPCHAIN012`. A syntax error still propagates as `json.JSONDecodeError`, which the callers wrap. A reference path the OS path call rejects with `ValueError` (a NUL, or on POSIX a lone surrogate) is a plain `ReferenceResolverError` from `validate_ref_path`.

### Security Features
- `max_parent_traversal_depth`: Limits `..` in paths (default: 3)
- `root_path`: Constrains references to stay within a directory tree

### Circular Reference Detection
- External references are tracked by `(file, pointer)` and inherited down the resolution chain, so cross-document cycles (A → B → A) are detected.
- Internal references (`#/pointer`) are document-local: they are tracked per document and are **not** inherited across a file boundary. Two documents that reuse the same pointer string are not a cycle; a genuine intra-document cycle (`#/a → #/b → #/a`) is still detected.
- Raises `ReferenceResolverError` on circular dependency detection.
