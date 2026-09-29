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
    needs_rendering,
    is_complete_template,
    extract_template_expression,
    find_templates,
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
# collection time. An escaped \{{ is no template.
contains_template({"a": ["{{ x }}"]})  # True
contains_template("\\{{ x }}")  # False

# Whether rendering changes anything in the structure: a template, or an
# escape, which only rendering removes. What a caller asks before it skips
# rendering (the carrier's models, a vars value, walk()'s own model step)
needs_rendering("\\{{ x }}")  # True

# Check if string is a complete template
is_complete_template("{{ value }}")  # True
is_complete_template("Hello {{ name }}")  # False

# Extract expression from template
extract_template_expression("{{ value }}")  # "value"

# The templates in a string, as the engine renders them: TEMPLATE_PATTERN's
# matches with an `expr` (escapes and escaped text left out)
[m.group("expr") for m in find_templates("\\{{ a {{ b }} }} {{ c }}")]  # [" c "]
```

More exports are part of the public surface (the main plugin's models and
validator depend on them, so treat them as API, not internals):

- `TEMPLATE_PATTERN` — the compiled-ready regex that defines `{{ ... }}`
  syntax, escapes included (see Literal braces): the tokens rendering
  rewrites, left to right, each a template (named `expr` group set) or an
  escape. `find_templates(text)` is its templates, exactly those the engine
  renders; ask it (or `contains_template`), never a bare `re.search` with the
  pattern, which finds the escapes too. The single source of truth shared by
  the engine, the models (to type a field as a template), and the validator.
  `TEMPLATE_PATTERN_ECMA` is its braces and expression for the JSON Schema's
  complete-template `pattern`s, anchored after whitespace, where no escape
  can be.
- `contains_escape(text)`, `unescape(text)` — whether rendering rewrites more
  of a string than its templates (a backslash right before `{{`), and what it
  makes of the escapes, templates left as written: the text a string without
  templates renders to. The models judge a literal by it (`types.as_rendered`)
  and keep the text as written, which the engine renders once; `validate
  --deep` names the file a path with only escapes renders to.
- `escape(text)` — the inverse: the scenario text that renders to `text`,
  each `{{` escaped and the backslashes right before it doubled, so nothing in
  it is a template (`unescape(escape(t)) == t`). For a tool writing values it
  did not author into a scenario (`importers`, which write recorded traffic).
  `escape(text, closed=True)` is for text a template follows on its line: an
  escape runs to the first `}}` on its line, so a `{{` no `}}` follows is
  written `{{ '{{' }}` instead, a template rendering to the braces, and no
  escape reaches past the text (it holds templates then). Text that would be
  that template alone with whitespace around it (`" {{"`) is one template of
  the whole text (`{{ ' {{' }}`): a string that is one template renders to
  its value, the whitespace dropped. A caller splits its text at the
  templates it writes and escapes each piece (the importers' `_joined_text`):
  a `}}` after a template closes no escape before it.
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

### Literal braces
A backslash escapes the `{{` right after it: `\{{` renders as the text `{{`,
and so does the text after it up to the first `}}` on its line (the rest of
the line when none), which opens no template: `\{{{{ x }}` is `{{{{ x }}`,
`\{{ a {{ x }} }}` is `{{ a {{ x }} }}`, `\{{ '{{' }}` is `{{ '{{' }}`: escaping
a tag escapes all of it, so a Handlebars raw block or a Jinja payload's own
literal braces take one backslash per tag. After that `}}` the text is read as
usual. (Handlebars itself ends escaped text at the next `{{`, and evaluates
the `x` of `\{{{{ x }}`; the escape here is specified to run to the `}}`.)
Before `{{`, `\\` stands for one
backslash, so a run of backslashes there renders halved, in escaped text too,
and an odd one escapes the braces: `\\{{ x }}` is a backslash, then x's value
(interpolated, never the value itself: `is_complete_template` refuses a
backslash before the braces). A `}}` needs no escape, a backslash anywhere else
is text, and a template's expression is read as written (`{{ '\{{' }}` renders
`\{{`). The expression form `{{ '{{' }}` renders `{{` too.

`TEMPLATE_PATTERN` carries the rule, so every consumer agrees on it. It is a
tokenizer, not a template finder: whether a `{{` is in escaped text depends on
what comes before it on its line, which no lookbehind can read, so it matches
templates and escapes alike, scanned left to right without overlap (a
template's match takes in the even run of backslashes before its braces, as
`backslashes`; an escape's is the odd run, the braces and the text they
escape; an even run before braces that open no template is one too), and a
lookbehind starts each at a run's start. `find_templates` keeps the
templates. Rendering (`render_text`) is one `re.sub` with it, left to right:
what a template renders to is never read again, so a value (a save, a
variable, a fixture's) holding `{{ x }}` or `\{{` is put in as it is, and an
escape is removed exactly once. Keys are not rendered (`_walk` maps values
only), so a key keeps its escape as written.

`contains_template` and `needs_rendering` differ by the escape: a string
holding only an escaped `\{{` is no template (its value is known before it
renders: no collection-time resolution, no model typing it a template), but
does need rendering, so every short-circuit that skips rendering (`_walk`'s
model and namespace steps, the carrier's `_render_declared` and verify
renderer, `utils.process_substitutions`) asks `needs_rendering`.

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
- `SimpleNamespace`: Processes namespace attributes, rebuilt as its own type (`copy.replace`), so a `vars` object (the models' `VarsNamespace`, a SimpleNamespace that is a registered `Mapping`) keeps its key access once rendered
- A model or namespace with nothing to render (`needs_rendering`) is handed back as it is

### Objects read by key
A subscript of a key the object (a `vars` object, a dict, headers) does not have fails as `Key error in expression '{{ user['nick'] }}': Key 'nick' does not exist in expression ...`, as the attribute path's `Attribute error` names an attribute: `_Evaluator` overrides simpleeval's `_eval_subscript`, which lets the bare KeyError out, and catches only the lookup's own KeyError (a user function's raising one is still `KeyError in expression`). simpleeval checks nothing of a subscript's key, so `doc['_id']` reads a key the attribute form refuses. An attribute a `vars` object has no key for falls through to its method of that name (`order.items` where `order` has no `items` key). `_Evaluator._eval_attribute` lets that method through only where the expression calls it (`order.items()`) or hands it as a call's `key=` (`max(s, key=s.get)`, the argument `scoping` takes for a function), read off the tree being evaluated (`_takes_a_method`); anywhere else, wherever it sits (`{{ order.items != [] }}`, `bool(order.get)`, `str(order.keys)`), it is refused as the missing attribute it was before the object had methods (`_MethodNotCalled`, an `AttributeDoesNotExist` whose message adds how to call the method), so a check reading it fails rather than pass on the method, and no text gets its repr, the whole object in it. `_is_object_method` tells such a method (a bound method of a SimpleNamespace that is a Mapping, so the package need not know the class by name); a fixture object's methods are left alone. A dict's are too: simpleeval reads a dict's method before its key, so `saved.items` on a saved object is the dict's method, key or no key, as it always was.

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
