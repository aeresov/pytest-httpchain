# pytest_httpchain.templates

Template substitution engine using `{{ expression }}` syntax for pytest-httpchain.

## Purpose

This subpackage provides safe template expression evaluation with recursive substitution support for:
- Strings with embedded expressions (`"Hello {{ name }}"`)
- Dictionaries, lists, Pydantic models, and SimpleNamespace objects
- Python expressions including list/dict comprehensions

## Public API

```python
from pytest_httpchain.templates import (
    walk,
    walker,
    contains_template,
    is_complete_template,
    extract_template_expression,
    TEMPLATE_PATTERN,
    TEMPLATE_BUILTINS,
    TemplatesError,
)

# Recursively substitute template expressions
result = walk(obj, context)

# walk() bound to one context. It builds the simpleeval evaluator (a full
# pass over the context) once, and every call reuses it. Use it to render
# many values against the same context, each on its own: a call that raises
# its TemplatesError leaves the walker as it found it. walk() pays that pass
# once per call, and at runtime the context is a ChainMap of a layer per stage
# and per save step — the carrier renders a verify step's values this way, one
# failure per value. Not thread-safe (simpleeval mutates the evaluator
# mid-eval): one walker per thread or parallel iteration, never a shared one.
render = walker(context)
first, second = render(obj_a), render(obj_b)

# Check whether any {{ }} occurs anywhere in a nested structure
# (str/dict/list/tuple/BaseModel/SimpleNamespace) — carrier uses this to decide
# whether stage parametrization forces scenario substitutions to resolve at
# collection time
contains_template({"a": ["{{ x }}"]})  # True

# Check if string is a complete template
is_complete_template("{{ value }}")  # True
is_complete_template("Hello {{ name }}")  # False

# Extract expression from template
extract_template_expression("{{ value }}")  # "value"
```

More exports are part of the public surface (the main plugin's models and
validator depend on them, so treat them as API, not internals):

- `TEMPLATE_PATTERN` — the compiled-ready regex (with named `expr` group) that
  defines `{{ ... }}` syntax. The single source of truth shared by the engine,
  the models (to type a field as a template), and the validator.
- `TEMPLATE_BUILTINS` — the set of names available inside an expression without
  the user defining them (safe functions, the helpers of `functions.py`, JSON
  literals, `exists`/`get`, and simpleeval defaults). The validator uses it to
  tell a genuine typo from an engine-provided name.
- `CONTEXT_HELPERS` — `exists`/`get`, the built-ins a user callable never
  shadows; `CALL_ONLY_BUILTINS` — the built-ins of no use but called (the
  `functions.py` helpers, `uuid4`, `env`, `rand`, `randint`), which are refused
  when a template renders to one uncalled. The validator's reference model
  (`scoping`) reads both. `call_form(name)` writes the call its advice names:
  `now()`, or `env(...)` for one that takes arguments.
- `parse_expression(text)` — the `ast.expr` a template's text holds, parsed
  exactly as the engine evaluates it, or a `TemplatesError` whose message is
  the reason the engine gives for refusing it (the runtime prefixes
  `Invalid expression '{{ ... }}': `). The validator's single source for
  "does this template evaluate at all" and for the names it reads.
  `template_form(text)` writes the template as those messages name it,
  `{{ text }}`, a lone surrogate escaped (`\ud800`) so that any UTF-8 stream
  can print it; the validator's HTTPCHAIN037/038 use it too.

## Key Behaviors

### Template Syntax
- Template expressions use `{{ expression }}` syntax
- Single expressions preserve type: `walk("{{ 42 }}", {})` returns `42` (int)
- Surrounding whitespace still counts as a single expression: `walk(" {{ 42 }} ", {})` returns `42` (int), not `" 42 "`. The whole-string check (`_sub_string`) uses the same whitespace-tolerant predicate (`extract_template_expression`) as `is_complete_template`, which the models use to type a field as `TemplateExpression` — so schema validation and runtime evaluation agree. The padding (spaces, tabs, newlines) is dropped.
- Mixed content returns string: `walk("Value: {{ 42 }}", {})` returns `"Value: 42"`
- Single-line only: the pattern is not compiled with `re.DOTALL`, so an expression spanning newlines is not recognised as a template. Keep each `{{ ... }}` on one line (move multi-line logic into a user function).
- One expression per template: `walk("{{ a; b }}", ...)` raises `TemplatesError`, and so does an assignment (`{{ x = 1 }}`, `+=`, an annotated `x: int = 1`, `:=`) or any other statement. simpleeval parses in exec mode and would evaluate only `a` behind a `MultipleExpressions` warning, and an `=` or `+=` as its right-hand side behind an `AssignmentAttempted` one, so `_eval_expr` evaluates only what `parse_expression` returns: the one expression, or a `TemplatesError` saying why the text is none (a syntax error, empty, `a; b`, an assignment, a statement, too deeply nested to parse, a lone surrogate the parser cannot encode). The validator reads the same parse: such a template is its finding `HTTPCHAIN037` (`HTTPCHAIN038`, an error, where it renders before any stage runs: scenario level, a parametrize value), and names nothing to the reference checks (`scoping`).
- What simpleeval refuses from the text alone is refused by `parse_expression` too, wherever in the tree it sits (an untaken branch included), so that `validate` sees it: an expression kind its evaluator does not dispatch (`_EVALUATED_KINDS`, read off an `EvalWithCompoundTypes` so it follows simpleeval's version: a lambda, a set comprehension, `yield`, `await`, `*` unpacking except as an element of a list literal, which `_eval_list` spreads itself), an attribute named with a `DISALLOW_PREFIXES` prefix (`_`, `func_`) or in `DISALLOW_METHODS` (`format`, `mro`, ...), and a call of anything but a name or an attribute (simpleeval's "Lambda Functions not implemented"). simpleeval still refuses each when it evaluates, so its guards do not come to rest on this parse alone (a test pins that it does). What it refuses for a value (a module, a function in `DISALLOW_FUNCTIONS`) is left to it.

### Trailing `}}` in dict/set literals (gotcha)
The template delimiter is `}}`, and the matcher stops at the first `}}`. So a dict or set literal whose own closing brace sits immediately before the template's closing braces produces three consecutive `}` (`...}}}`), and the expression is truncated at the wrong place — the result is a broken evaluation (`'{' was never closed`, which `validate` reports as `HTTPCHAIN037` before any request is sent), or, where the truncated text happens to parse, a wrong one.

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

### Nesting depth
`contains_template` walks iteratively, so any depth works. `walk` rebuilds the
structure and still recurses once per level of nesting (a namespace included).
A value nested past the interpreter's recursion limit fails as `TemplatesError`
("Value nested too deeply to substitute"), which callers already report as a
stage failure, never as a bare `RecursionError`.

### Built-in Functions
Safe functions available in expressions:
- Type conversion: `bool`, `int`, `float`, `str`, `dict`, `list`, `tuple`, `set`
- Math: `min`, `max`, `sum`, `abs`, `round`, `rand()`, `randint(top)`
- Collections: `len`, `sorted`, `enumerate`, `zip`, `range`
- Utilities: `uuid4()`, `env(var, default)`
- Context helpers: `get(var, default)`, `exists(var)`
- Time: `now(fmt=None)` (UTC, ISO 8601 with offset and microseconds, or `strftime(fmt)`; `%s` refused, as the C library formats it in local time), `timestamp()`, `timestamp_ms()`
- Encoding: `b64encode(value, urlsafe=False)`, `b64decode(value, urlsafe=False)` (padding optional, UTF-8 result), `json_dumps(value)` (json.dumps defaults, a `vars` namespace encoded as its object), `json_loads(text)`
- URLs: `urlencode(mapping)` (encoded as httpx encodes `request.params`: list values repeat the key, `true`/`false`, empty for None; bytes percent-encoded as they are, where httpx sends their repr; nested objects and functions refused), `quote(text, safe='')`
- Hashing: `sha256(value)`, `md5(value)`, `hmac_sha256(key, message, encoding='hex')` (`'hex'` or `'base64'`)

The time, encoding, URL and hashing helpers live in `functions.py`
(`HELPER_FUNCTIONS`, merged into `SAFE_FUNCTIONS`, so `TEMPLATE_BUILTINS`
covers them). Each returns a plain value (text, a number, JSON data), never an
object of a class of its own, so they add nothing an expression can reach
through; the function objects themselves are reachable as names like every
built-in, and simpleeval refuses their dunder attributes. Text arguments are
encoded as UTF-8, and a number where text or bytes is expected is refused, not
`str()`-ed. A bad argument raises a TypeError or ValueError naming the helper,
which `_eval_expr` wraps as the template's `TemplatesError`. An expression
that evaluates to a call-only built-in itself (`CALL_ONLY_BUILTINS`: a helper,
`uuid4`, `env`, `rand`, `randint`; `{{ now }}`, parentheses forgotten) is a
`TemplatesError` too ("Uncalled function"), not its repr in a request. The
check is by identity, since a value may be unhashable. The same holds where
the user's own value of that name is missing (a save that has not landed), so
the message says no value of the name is defined there before it says to call
the built-in. The validator warns of the mistake statically (HTTPCHAIN035),
`str(now)` and `dict(at=now)` included. `env` is a function of the engine's,
not `os.environ.get`: that bound method's repr lists the whole environment,
which `str(env)` would have put in a request.

User names shadow the built-ins (the evaluator's merge order), except
`exists`/`get` (`CONTEXT_HELPERS`), which are merged last so that a call always
reaches them: a read (`{{ now }}`) finds a user value first, a call
(`{{ now() }}`) a user callable, and a call under a user *value* still reaches
the built-in. `scoping` models this statically: a built-in's name counts as a
reference (`extract_template_variables`) where the scenario defines that name
and a template reads it; a call under it is none. A name used only as a
function, called where the scenario defines it as a fixture or function
substitution (a possible callable; `exists`/`get` never), handed as a `key=`
(`key=len`) where the scenario defines it at all, or handed to the user's own
function or a method of a fixture's object (`sign(now)`, `helper.ids(uuid4)`)
where the scenario defines it as a possible callable, never fails out of
the scope of the user's definition, since the built-in stands in:
`extract_builtin_stand_ins` has those, which the validator reports out of
scope as a warning (HTTPCHAIN036), never as an undefined name.

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
- Evaluation errors are raised as `TemplatesError` — including the `str()` that interpolates a value into a string, which can raise too (an int past Python's digit limit, a failing `__str__`).
