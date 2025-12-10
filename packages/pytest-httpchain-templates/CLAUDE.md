# pytest-httpchain-templates

Template substitution library using `{{ expression }}` syntax for pytest-httpchain.

## Purpose

This package provides safe template expression evaluation with recursive substitution support for:
- Strings with embedded expressions (`"Hello {{ name }}"`)
- Dictionaries, lists, Pydantic models, and SimpleNamespace objects
- Python expressions including list/dict comprehensions

## Package Structure

```
src/pytest_httpchain_templates/
├── __init__.py          # Public API: walk, is_complete_template, extract_template_expression, TemplatesError
├── substitution.py      # Main walk() function for recursive substitution
├── expressions.py       # Template pattern matching utilities
└── exceptions.py        # TemplatesError exception
```

## Public API

```python
from pytest_httpchain_templates import walk, is_complete_template, extract_template_expression, TemplatesError

# Recursively substitute template expressions
result = walk(obj, context)

# Check if string is a complete template
is_complete_template("{{ value }}")  # True
is_complete_template("Hello {{ name }}")  # False

# Extract expression from template
extract_template_expression("{{ value }}")  # "value"
```

## Key Behaviors

### Template Syntax
- Template expressions use `{{ expression }}` syntax
- Single expressions preserve type: `walk("{{ 42 }}", {})` returns `42` (int)
- Mixed content returns string: `walk("Value: {{ 42 }}", {})` returns `"Value: 42"`

### Supported Object Types
- `str`: Substitutes template expressions
- `dict`: Recursively processes values
- `list`: Recursively processes items
- `BaseModel` (Pydantic): Dumps, processes, and revalidates
- `SimpleNamespace`: Processes namespace attributes

### Built-in Functions
Safe functions available in expressions:
- Type conversion: `bool`, `dict`, `list`, `tuple`, `set`
- Math: `min`, `max`, `sum`, `abs`, `round`
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

### Security
- Uses `simpleeval` for safe expression evaluation
- Blocks dangerous operations (`__import__`, `open`, etc.)
- Raises `TemplatesError` for evaluation errors

## Running Tests

```bash
# From monorepo root
uv run pytest packages/pytest-httpchain-templates/tests -v

# Or from package directory
cd packages/pytest-httpchain-templates
uv run pytest tests -v
```

## Error Handling

All errors raise `TemplatesError` with descriptive messages:
- Undefined variable
- Unknown function
- Attribute error
- Invalid expression (syntax errors)
- Runtime errors (ZeroDivisionError, TypeError, IndexError, KeyError)
