# pytest-httpchain-templates

Template substitution library using `{{ expression }}` syntax for pytest-httpchain.

## Purpose

This package provides safe template expression evaluation with recursive substitution support for:
- Strings with embedded expressions (`"Hello {{ name }}"`)
- Dictionaries, lists, Pydantic models, and SimpleNamespace objects
- Python expressions including list/dict comprehensions

## Package Structure

```
src/pytest_httpchain/templates/
├── __init__.py          # Public API: walk, is_complete_template, extract_template_expression, TEMPLATE_PATTERN, TEMPLATE_BUILTINS, TemplatesError
├── substitution.py      # Main walk() function for recursive substitution
├── expressions.py       # Template pattern matching utilities
└── exceptions.py        # TemplatesError exception
```

## Public API

```python
from pytest_httpchain.templates import (
    walk,
    is_complete_template,
    extract_template_expression,
    TEMPLATE_PATTERN,
    TEMPLATE_BUILTINS,
    TemplatesError,
)

# Recursively substitute template expressions
result = walk(obj, context)

# Check if string is a complete template
is_complete_template("{{ value }}")  # True
is_complete_template("Hello {{ name }}")  # False

# Extract expression from template
extract_template_expression("{{ value }}")  # "value"
```

Two more exports are part of the public surface (the main plugin's models and
validator depend on them, so treat them as API, not internals):

- `TEMPLATE_PATTERN` — the compiled-ready regex (with named `expr` group) that
  defines `{{ ... }}` syntax. The single source of truth shared by the engine,
  the models (to type a field as a template), and the validator.
- `TEMPLATE_BUILTINS` — the set of names available inside an expression without
  the user defining them (safe functions, JSON literals, `exists`/`get`, and
  simpleeval defaults). The validator uses it to tell a genuine typo from an
  engine-provided name.

## Key Behaviors

### Template Syntax
- Template expressions use `{{ expression }}` syntax
- Single expressions preserve type: `walk("{{ 42 }}", {})` returns `42` (int)
- Surrounding whitespace still counts as a single expression: `walk(" {{ 42 }} ", {})` returns `42` (int), not `" 42 "`. The whole-string check (`_sub_string`) uses the same whitespace-tolerant predicate (`extract_template_expression`) as `is_complete_template`, which the models use to type a field as `TemplateExpression` — so schema validation and runtime evaluation agree. The padding (spaces, tabs, newlines) is dropped.
- Mixed content returns string: `walk("Value: {{ 42 }}", {})` returns `"Value: 42"`
- Single-line only: the pattern is not compiled with `re.DOTALL`, so an expression spanning newlines is not recognised as a template. Keep each `{{ ... }}` on one line (move multi-line logic into a user function).

### Trailing `}}` in dict/set literals (gotcha)
The template delimiter is `}}`, and the matcher stops at the first `}}`. So a dict or set literal whose own closing brace sits immediately before the template's closing braces produces three consecutive `}` (`...}}}`), and the expression is truncated at the wrong place — the result is a broken or wrong evaluation, not an error you can easily spot.

Always put a space between a literal's closing `}` and the template's closing `}}`:

```python
# WRONG — `{'key': value}}}` truncates: the matcher closes the template early
walk("{{ {'key': value}}}", {"value": 1})

# RIGHT — space before the closing }} keeps the dict literal intact
walk("{{ {'key': value} }}", {"value": 1})  # {"key": 1}

# Same rule for nested literals: space before the outer }}
walk("{{ {'outer': {'inner': id} } }}", {"id": "x"})  # {"outer": {"inner": "x"}}
```

This only affects a literal `}` that is adjacent to the template close; a single `}` elsewhere in the expression (including inside a string, e.g. `{{ '} ' + msg }}`) is fine.

### Supported Object Types
- `str`: Substitutes template expressions
- `dict`: Recursively processes values
- `list`: Recursively processes items
- `BaseModel` (Pydantic): Dumps, processes, and revalidates
- `SimpleNamespace`: Processes namespace attributes

### Built-in Functions
Safe functions available in expressions:
- Type conversion: `bool`, `int`, `float`, `str`, `dict`, `list`, `tuple`, `set`
- Math: `min`, `max`, `sum`, `abs`, `round`, `rand()`, `randint(top)`
- Collections: `len`, `sorted`, `enumerate`, `zip`, `range`
- Utilities: `uuid4()`, `env(var, default)`
- Context helpers: `get(var, default)`, `exists(var)`

### JSON-style Literals
For compatibility with JSON syntax, lowercase boolean literals are supported:
- `true` → `True`
- `false` → `False`
- `null` → `None`

### Expression Examples
```python
# Simple substitution
walk("{{ name }}", {"name": "Alice"})  # "Alice"

# Comprehensions
walk("{{ [x * 2 for x in items] }}", {"items": [1, 2, 3]})  # [2, 4, 6]

# Safe variable access
walk("{{ get('missing', 'default') }}", {})  # "default"
walk("{{ exists('var') }}", {"var": 1})  # True

# Environment variables
walk("{{ env('HOME', '/tmp') }}", {})  # value of $HOME or "/tmp"
```

### Trust model
- Scenario files are **trusted** input: treat them like code, not like untrusted data. Anyone who can author or edit a scenario can run arbitrary Python via templates.
- `simpleeval` reduces accidental footguns (it rejects `__import__`, `open`, dunder/attribute access, etc.), but it is **not** a hardened sandbox. Upstream explicitly disclaims sandboxing, so do **not** rely on it as a security boundary against hostile expressions.
- `env()` exposes the entire process environment, and context callables (user functions, factory fixtures) execute arbitrary Python by design.
- Evaluation errors are raised as `TemplatesError`.

## Running Tests

```bash
uv run pytest tests/unit/templates -v
```

## Error Handling

All errors raise `TemplatesError` with descriptive messages:
- Undefined variable
- Unknown function
- Attribute error
- Invalid expression (syntax errors)
- Runtime errors (ZeroDivisionError, TypeError, IndexError, KeyError)
