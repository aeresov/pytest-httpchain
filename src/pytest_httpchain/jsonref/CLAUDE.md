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

## Subpackage Structure

```
src/pytest_httpchain/jsonref/
├── __init__.py          # Public API: load_json, ReferenceResolverError
├── loader.py            # Main entry point: load_json()
├── exceptions.py        # ReferenceResolverError exception
└── plumbing/            # Internal implementation
```

## Public API

```python
from pytest_httpchain.jsonref import load_json, ReferenceResolverError

# Load JSON with $ref resolution
data = load_json(path, max_parent_traversal_depth=3, root_path=None)
```

## Key Behaviors

### Reference Resolution
All three directives (`$include`, `$merge`, `$ref`) work identically:
- External refs: `{"$include": "file.json"}` loads and merges entire file
- Pointer refs: `{"$include": "#/path/to/node"}` references within same document
- Combined: `{"$include": "file.json#/path"}` references specific node in external file

### Deep Merging
When `$include` (or `$ref`) has sibling properties, they are merged **additively** with the referenced content: sibling keys are added, lists are **concatenated**, and nested dicts are merged recursively. There is **no** last-wins override — a sibling that would override an existing scalar (or conflicts by type) raises `ReferenceResolverError` (`Merge conflict at <path>`) rather than silently winning.
```json
{
  "$include": "base.json",
  "extra": "value"  // merged with referenced content
}
```

### Security Features
- `max_parent_traversal_depth`: Limits `..` in paths (default: 3)
- `root_path`: Constrains references to stay within a directory tree

### Circular Reference Detection
- External references are tracked by `(file, pointer)` and inherited down the resolution chain, so cross-document cycles (A → B → A) are detected.
- Internal references (`#/pointer`) are document-local: they are tracked per document and are **not** inherited across a file boundary. Two documents that reuse the same pointer string are not a cycle; a genuine intra-document cycle (`#/a → #/b → #/a`) is still detected.
- Raises `ReferenceResolverError` on circular dependency detection.

## Running Tests

```bash
uv run pytest tests/unit/jsonref -v
```

## Common Patterns

### Error Handling
All resolution errors raise `ReferenceResolverError` with descriptive messages including:
- Invalid `$ref` format
- File not found (shows all paths tried)
- Invalid JSON pointer
- Merge conflicts between incompatible types
- Circular references
