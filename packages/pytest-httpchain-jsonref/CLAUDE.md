# pytest-httpchain-jsonref

JSON reference (`$ref`) resolution library for pytest-httpchain.

## Purpose

This package provides JSON file loading with full `$ref` resolution support, including:
- External file references (`"$ref": "other.json"`)
- JSON pointer references (`"$ref": "#/definitions/foo"`)
- Combined references (`"$ref": "other.json#/definitions/foo"`)
- Deep merging of sibling properties with referenced content

## Package Structure

```
src/pytest_httpchain_jsonref/
├── __init__.py          # Public API: load_json, ReferenceResolverError
├── loader.py            # Main entry point: load_json()
├── exceptions.py        # ReferenceResolverError exception
└── plumbing/            # Internal implementation
```

## Public API

```python
from pytest_httpchain_jsonref import load_json, ReferenceResolverError

# Load JSON with $ref resolution
data = load_json(path, max_parent_traversal_depth=3, root_path=None)
```

## Key Behaviors

### Reference Resolution
- External refs: `{"$ref": "file.json"}` loads and merges entire file
- Pointer refs: `{"$ref": "#/path/to/node"}` references within same document
- Combined: `{"$ref": "file.json#/path"}` references specific node in external file

### Deep Merging
When `$ref` has sibling properties, they are deep-merged using `deepmerge.always_merger`:
```json
{
  "$ref": "base.json",
  "extra": "value"  // merged with referenced content
}
```

### Security Features
- `max_parent_traversal_depth`: Limits `..` in paths (default: 3)
- `root_path`: Constrains references to stay within a directory tree

### Circular Reference Detection
- Tracks both external (file+pointer) and internal (pointer-only) references
- Raises `RuntimeError` on circular dependency detection

## Running Tests

```bash
# From monorepo root
uv run pytest packages/pytest-httpchain-jsonref/tests -v

# Or from package directory
cd packages/pytest-httpchain-jsonref
uv run pytest tests -v
```

## Common Patterns

### Error Handling
All resolution errors raise `ReferenceResolverError` with descriptive messages including:
- Invalid `$ref` format
- File not found (shows all paths tried)
- Invalid JSON pointer
- Merge conflicts between incompatible types
- Circular references (raises `RuntimeError`)
