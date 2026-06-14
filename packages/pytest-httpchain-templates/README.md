# pytest-httpchain-templates

The `{{ expression }}` template engine for
[pytest-httpchain](https://github.com/aeresov/pytest-httpchain).

This package evaluates template expressions and walks them recursively through
strings, dicts, lists, Pydantic models, and `SimpleNamespace` objects against a
context. A value that is a single `{{ ... }}` expression preserves its type
(`walk("{{ 42 }}", {})` returns the int `42`), while mixed content interpolates
into a string. Expressions are evaluated with `simpleeval` and a curated set of
built-ins (type conversions, `len`/`min`/`max`/`sum`, `range`, `uuid4()`,
`env()`, and the context helpers `get()` / `exists()`). Note that scenario files
are treated as trusted input — `simpleeval` blocks accidental footguns but is
not a hardened sandbox, so templates can run arbitrary Python by design.

## Role in the workspace

pytest-httpchain shares a key-value context across the stages of a scenario.
When a stage runs, the plugin walks its request model through this engine,
substituting saved values, fixtures, parametrize parameters, and substitution
variables before the HTTP request is sent. It is published separately so the
evaluation logic can be reused and tested in isolation.

## Usage

```python
from pytest_httpchain_templates.substitution import walk
from pytest_httpchain_templates.exceptions import TemplatesError

try:
    walk("{{ name }}", {"name": "Alice"})            # "Alice"
    walk("{{ [x * 2 for x in items] }}", {"items": [1, 2]})  # [2, 4]
    walk("{{ get('missing', 'default') }}", {})      # "default"
except TemplatesError as e:
    print(f"template error: {e}")
```

## Links

- Documentation: <https://aeresov.github.io/pytest-httpchain/>
- Source and issues: <https://github.com/aeresov/pytest-httpchain>
