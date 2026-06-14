# pytest-httpchain-jsonref

JSON reference (`$ref`) resolution with deep merging, used by
[pytest-httpchain](https://github.com/aeresov/pytest-httpchain).

This package loads a JSON document and resolves reference directives into a
single in-memory structure. It supports external file references, JSON-pointer
references within a document, and combined `file.json#/pointer` references.
Sibling properties next to a directive are merged *additively* with the
referenced content (keys added, lists concatenated, dicts merged recursively;
a conflicting scalar raises rather than silently overriding). It also enforces a
few safety constraints — a parent-traversal depth limit, an optional root
directory that references may not escape, and circular-reference detection.

## Role in the workspace

pytest-httpchain lets an HTTP test scenario be split across reusable JSON
fragments. At collection time the plugin calls this package to inline every
`$ref` / `$include` / `$merge` directive before the resulting document is
validated against the
[models](https://pypi.org/project/pytest-httpchain-models/). It is published
separately so the resolution logic can be reused and tested on its own; it is
not intended to be used directly outside the plugin.

## Usage

```python
from pytest_httpchain_jsonref import load_json, ReferenceResolverError

try:
    data = load_json("scenario.json", max_parent_traversal_depth=3, root_path="tests")
except ReferenceResolverError as e:
    print(f"could not resolve references: {e}")
```

Three directives are accepted and behave identically — `$include` and `$merge`
are preferred (they avoid clashing with editor JSON Schema validation), while
`$ref` is the legacy spelling:

```json
{
    "$include": "base.json#/definitions/login",
    "extra": "merged with the referenced content"
}
```

## Links

- Documentation: <https://aeresov.github.io/pytest-httpchain/>
- Source and issues: <https://github.com/aeresov/pytest-httpchain>
