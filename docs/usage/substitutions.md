# Substitutions and Templates

## Common Data Context

pytest-httpchain maintains a key-value context throughout scenario execution. This context is populated by:

1. Variables from `substitutions`
2. pytest fixtures
3. Values saved from responses

## Substitutions Structure

Substitutions can be defined at scenario or stage level:

```json
{
    "substitutions": [
        {
            "vars": {
                "base_url": "https://api.example.com",
                "timeout": 30
            }
        },
        {
            "functions": {
                "sequence": "mymodule:next_sequence"
            }
        }
    ]
}
```

### List vs Dictionary Format

**List format:**

```json
{
    "substitutions": [
        {"vars": {"key1": "value1"}},
        {"vars": {"key2": "value2"}}
    ]
}
```

**Dictionary format** (keys are organizational only):

```json
{
    "substitutions": {
        "config": {
            "vars": {
                "base_url": "https://api.example.com",
                "api_version": "v2"
            }
        },
        "auth": {
            "functions": {
                "token": "auth_module:get_token"
            }
        }
    }
}
```

**Mixed format:**

```json
{
    "substitutions": {
        "batch1": [
            {"vars": {"key1": "value1"}},
            {"vars": {"key2": "value2"}}
        ],
        "batch2": {"vars": {"key3": "value3"}}
    }
}
```

## Variable Substitutions

Define static values:

```json
{
    "substitutions": [
        {
            "vars": {
                "string_var": "hello",
                "number_var": 42,
                "bool_var": true,
                "list_var": [1, 2, 3],
                "object_var": {"nested": "value"}
            }
        }
    ]
}
```

Where a field takes a whole object, one template naming it stands for that
object: a JSON body (`"json": "{{ object_var }}"`), GraphQL `variables`,
`verify.body.schema`, a header matcher, and the `combinations` of a
`parametrize` or `parallel.foreach` step.

### Reading objects

A template reads an object in `vars`, at any depth and in lists too, by
attribute or by key, the way it reads an object a `save` took from a response:

```json
{
    "substitutions": [
        {
            "vars": {
                "user": {"name": "Alice", "roles": ["admin"], "address": {"city": "Oslo"}},
                "trace": {"X-Request-Id": "req-42", "_id": 7}
            }
        }
    ]
}
```

| Template | Renders |
|----------|---------|
| `{{ user.name }}`, `{{ user['name'] }}` | `"Alice"` |
| `{{ user.address.city }}`, `{{ user['address']['city'] }}` | `"Oslo"` |
| `{{ trace['X-Request-Id'] }}` | `"req-42"`: a key that is no Python name reads only by key |
| `{{ trace['_id'] }}` | `7`: so does a key starting with `_`, which the engine never reads as an attribute |
| `{{ 'roles' in user }}`, `{{ 'nick' not in user }}` | `true` |
| `{{ len(user) }}` | `3` |
| `{{ [k for k in user] }}`, `{{ list(user.keys()) }}` | `["name", "roles", "address"]`: the keys, in the order written |
| `{{ list(user.values())[0] }}` | `"Alice"` |
| `{{ [k + '=' + str(v) for k, v in trace.items()] }}` | `["X-Request-Id=req-42", "_id=7"]` |
| `{{ user.get('nick', 'anon') }}`, `{{ user.get('nick') }}` | `"anon"`, `null` |
| `{{ dict(user)['name'] }}`, `{{ {k: user[k] for k in user}['name'] }}` | `"Alice"`: both copy the top level into a dict (`dict()` not for an object with a key named `keys`, see below) |

A key the object does not have fails the stage, naming it, as a missing
attribute does:
`Key error in expression '{{ user['nick'] }}': Key 'nick' does not exist in expression 'user['nick']'`.
For a key that may be missing, use `get()` or `in`. An empty object (`{}`) is
false in a condition (`or`, `and`, `... if ... else ...`, a template
`always_run`), as an empty saved object is; test `len(obj) == 0` or
`'key' in obj` when that is what you mean. The `response` metadata of a
response step reads the same way (`{{ response['status'] }}`).

**Keys named like the methods.** An attribute reads the object's key first, as
it always has, so for an object with a key named `keys`, `values`, `items` or
`get`, the attribute is that key's value: `{{ order.items }}` is the order's
items, and `{{ order.items() }}` fails, calling that value. Every form by key
reads the data, never a method, so use those for such an object:
`order['items']`, `list(order)` for the keys, `[order[k] for k in order]` for
the values, `[(k, order[k]) for k in order]` for the pairs. `dict(order)` and
`{**order}` call `order.keys()`, so for an object with a key named `keys` they
fail as well (`'str' object is not callable`); copy it with
`{k: order[k] for k in order}` (the top level) or `json_loads(json_dumps(order))`
(all the way down).

The other way round, for an object without such a key, the attribute reaches
the method only to call it (`order.items()`) or to hand it as a `key=`
(`{{ max(scores, key=scores.get) }}`, the key with the highest value).
Anywhere else, wherever it sits in the expression, `order.items` is the
missing attribute it always was, so a check such as `{{ order.items != [] }}`
or `{{ bool(order.get) }}` fails rather than pass on the method (a value that
is always true and never equal to data):
`Attribute error in expression '{{ order.items != [] }}': Attribute 'items' does not exist in expression 'order.items != []'; the object has no key 'items'; to call its method, write .items()`.
To test whether the object has a key, write `'items' in order`; to read it,
`order['items']` (a `Key error` where it is missing) or `order.get('items')`.

**A saved object's keys named like its methods.** An object a `save` took from
a response is a dict, and an attribute reads a dict's method before its key:
for a saved `{"items": 3}`, `{{ saved.items }}` is the dict's `items` method,
never `3`, and no error says so, with or without such a key. The same goes for
a key named like any other dict method (`keys`, `values`, `get`, `copy`, `pop`,
`update`, ...). For those keys the forms by key are the only safe ones on a
saved object: `saved['items']`, `saved.get('items')`.

An object from `vars` equals another with the same keys and values, but never a
dict, such as a saved object or a `{...}` literal: compare
`json_loads(json_dumps(user))` with it instead, or, for an object with no
object in it, `{k: user[k] for k in user}` (`dict(user)` too, unless the object
has a key named `keys`).

## Function Substitutions

Bind a name to a Python function so it can be **called** from templates:

```json
{
    "substitutions": [
        {
            "functions": {
                "uuid": "uuid:uuid4",
                "sequence": "mymodule:next_sequence",
                "config": "mymodule:load_config"
            }
        }
    ]
}
```

```python
# mymodule.py
import itertools

_counter = itertools.count(1)


def next_sequence() -> str:
    return f"seq-{next(_counter)}"


def load_config() -> dict:
    return {"environment": "test", "debug": True}
```

A function substitution seeds a **callable** under its alias — it is not invoked
when the substitution is processed. Call it with `()` in a template to get a
value; referencing it bare renders the function object itself:

```json
{
    "headers": {
        "X-Request-Id": "{{ uuid() }}",
        "X-Sequence": "{{ sequence() }}"
    },
    "body": {
        "json": {"environment": "{{ config()['environment'] }}"}
    }
}
```

Each call is re-evaluated per use (a fresh `uuid()` every time). A function that
returns a dict is accessed with subscript (`config()['environment']`).

## Template Expressions

Use `{{ expression }}` syntax in any request value. Expressions support Python syntax via simpleeval.

Each template holds exactly one expression: `{{ a; b }}` fails the stage
rather than evaluating only `a` (a `;` inside a string literal is fine), and so
does an assignment: `{{ user.active = True }}` fails with
`Invalid expression '{{ user.active = True }}': a template holds one
expression, not an assignment; to compare two values, write '=='`, rather than
evaluating to its right-hand side, which would pass that verify expression
whatever `user.active` is. `+=`, an annotated `x: int = 1`, `:=` and any other
statement fail too, each with a reason of its own.

The engine does not evaluate every Python expression either. A template fails
the stage, wherever in it the part sits, if it holds a lambda, a set
comprehension (write `set(x for x in items)`), `*` unpacking anywhere but in a
list literal (`[*a, *b]` works), `yield` or `await`; an attribute whose name
starts with `_` or `func_` (a JSON key such as `_id` is read as
`doc['_id']`, not `doc._id`), or a few others such as `format` (build text
with an f-string, `f'id-{n}'`, or `+`); or a call of anything but a name or an attribute
(`fns[0]()`).

`validate` and collection report each of these, and any syntax error, before
anything is sent, with the reason the stage would fail with: in a stage as the
warning `HTTPCHAIN037`, and as the error `HTTPCHAIN038` where nothing would run,
in a scenario-level template, which fails every stage, or a parametrize value,
which fails the scenario's collection. A common syntax error is a dict literal
whose closing `}` runs into the template's `}}`: `{{ {'a': 1}}}` ends the
template one brace early, so write `{{ {'a': 1} }}`.

Any error while evaluating a template, including turning an interpolated value
into text, fails the stage with a message naming the template. A template that is the
whole value keeps the value as it is; a query parameter's value is turned into
text only when the request is built, and a failure there names the parameter
(see [Requests](requests.md)).

!!! note
    Only **values** are substituted. A template in a dict *key* — a header name,
    a query parameter name, a JSON body key — is never rendered and is sent
    literally; the validator reports it as `HTTPCHAIN029`. Move the dynamic part
    into the value, or build the object in a user function.

### Literal braces

A `{{` in a value opens a template. To send the braces themselves (a
Handlebars or Mustache payload, documentation text, a server that expects a
`{{placeholder}}`), put a backslash before them: `\{{` is the text `{{`, and
so is everything after it up to the first `}}`, which opens no template. So
`\{{ name }}` is sent as `{{ name }}`, `name` never evaluated. In the JSON file
the backslash is escaped in turn, so it is written `\\{{`:

```json
{
    "request": {
        "url": "https://api.example.com/templates",
        "method": "POST",
        "headers": {"X-Template": "\\{{name}}"},
        "body": {
            "json": {
                "subject": "Hello \\{{user.name}}",
                "body": "\\{{#each items}}\\{{this}}\\{{/each}}"
            }
        }
    }
}
```

This sends the header `X-Template: {{name}}` and the body
`{"subject": "Hello {{user.name}}", "body": "{{#each items}}{{this}}{{/each}}"}`.
`validate` reads no template there either, so `name`, `user` and `items` are
not reported as undefined.

The escaped text runs to the first `}}` after the backslash, on the same
line, braces included, so template syntax inside it is text too: a Handlebars
raw block's `\{{{{raw}}}}`, a Jinja payload's own `\{{ '{{' }}`, or a nested
`\{{ a {{ b }} }}` is sent as written, one backslash less. After that `}}`,
text is read as usual again, so each tag of a payload takes its own backslash.
With no `}}` after it, the rest of the line is text.

Before `{{`, a doubled backslash stands for one: to put a backslash right
before a template, double it. Each pair of backslashes before `{{` renders as
one, and one left over escapes the braces; a backslash anywhere else is left
as it is (a Windows path's, a regex's `\d`), and a `}}` needs no escape at all:

| In the JSON file | Renders as |
|---|---|
| `"\\{{ id }}"` | `{{ id }}` |
| `"\\\\{{ id }}"` | a backslash, then the value of `id` |
| `"\\\\\\{{ id }}"` | `\{{ id }}` |
| `"\\{{{{ id }}"` | `{{{{ id }}` |
| `"\\{{ id }}{{ id }}"` | `{{ id }}`, then the value of `id` |
| `"\\{{{raw}}}"` | `{{{raw}}}` |

An expression renders the braces too: `{{ '{{' }}` is `{{`, and since what a
template renders is not read again, `"{{ '{{' }}name}}"` is `{{name}}`.

The escape is removed once, when the scenario's own text renders. A value a
template puts in, a saved response field, a variable or a fixture's value, is
put in as it is, braces and backslashes alike, and never rendered again. An
escaped template is text, so a field that takes only a whole template
(`timeout`, `skip_if`) refuses it as it refuses other text, and a verify
expression written that way is a string, not the boolean it needs
(`HTTPCHAIN018`).

Every value that renders takes the escape: request fields, verify operands
and `verify.jmespath` values, `save.regex` and `matches` patterns, `vars`
values and parametrize values. So does a file path (a `binary` body, an
upload, an `ssl` file), rendered once as any value is: `"uploads/\\{{name}}.txt"`
sends the file named `{{name}}.txt`, and `validate --deep` looks for that one.
Write a path's separator before `{{` as `/`, which Windows takes too: a `\`
there escapes the braces.

A literal is checked as the text it renders to, so a GraphQL query or a
JMESPath expression holding `\{{` in one of its strings is valid, although
neither has a `\{` escape of its own:
`"query { render(template: \"Hello \\{{name}}\") }"` sends
`query { render(template: "Hello {{name}}") }`.

Keys are never rendered, so a key keeps a `{{` and any backslash before it as
written, and so does a `functions` substitution's kwarg, which is passed
unrendered: braces there need no escape, and `validate` warns of one, which
would be sent with its backslash (`HTTPCHAIN029`, `HTTPCHAIN030`). The
client's `base_url` and `proxy`, and a `verify.body.schema` file reference,
take no literal braces: a backslash before `{{` in one fails validation.

In a regular expression, `\{{` renders `{{`, which Python's `re` reads as two
literal braces, as it reads `\{\{`.

!!! note "Migrating a backslash before a template"
    Before this escape existed, `\{{ x }}` in a value rendered a backslash and
    then the value of `x`. It now renders the text `{{ x }}`. Where the
    backslash is meant (a Windows path such as `"C:\\{{ dir }}"` in the JSON
    file), double it: `"C:\\\\{{ dir }}"`, or write the path with `/`.

### Basic Variable Access

```json
{
    "url": "{{ base_url }}/users/{{ user_id }}"
}
```

### String Operations

```json
{
    "headers": {
        "Authorization": "{{ 'Bearer ' + token }}",
        "X-Request-ID": "{{ prefix + '_' + str(counter) }}"
    }
}
```

### Arithmetic

```json
{
    "params": {
        "offset": "{{ page * page_size }}",
        "limit": "{{ page_size }}"
    }
}
```

### Conditionals

```json
{
    "headers": {
        "X-Debug": "{{ 'true' if debug_mode else 'false' }}"
    }
}
```

### Built-in Functions

Available functions in expressions:

-   `str()`, `int()`, `float()`, `bool()`
-   `len()`, `range()`
-   `list()`, `dict()`, `set()`, `tuple()`
-   `min()`, `max()`, `sum()`
-   `abs()`, `round()`
-   `sorted()`
-   `enumerate()`, `zip()`
-   `uuid4()`, `rand()`, `randint(top)`
-   `env(var, default)`
-   `get(var, default)`, `exists(var)`
-   time: `now()`, `timestamp()`, `timestamp_ms()`
-   encoding: `b64encode()`, `b64decode()`, `b64decode_bytes()`, `hex_bytes()`, `hexencode()`, `msgpack_pack()`, `json_dumps()`, `json_loads()`
-   URLs: `urlencode()`, `quote()`
-   hashing: `sha256()`, `md5()`, `hmac_sha256()`

The time, encoding, URL and hashing helpers cover the values a test otherwise
needs a fixture or a user function for. Each returns plain text, a number or,
for `json_loads`, JSON data. Call them with their parentheses: `{{ now }}`
alone is the function, not the time, so a template that renders to a helper
fails the stage ("Uncalled function ... call it: now()"), and `validate` warns
of a helper used without being called (`HTTPCHAIN035`), such as
`str(timestamp)` or `dict(at=timestamp)`. The same goes for `uuid4`, `env`,
`rand` and `randint`, which are no use uncalled either. A save named like one
of them fails the same way where it has not landed: `{{ quote }}` in an
`always_run` cleanup stage, after the stage that saves `quote` failed, gets
the built-in function, and the message says that no value named `quote` is
defined there.

#### Time

| Call | Returns |
| --- | --- |
| `now()` | The current UTC time in ISO 8601, with its offset and always with microseconds: `2026-09-27T12:34:56.789012+00:00` |
| `now(fmt)` | The current UTC time formatted with Python's `strftime`: `now('%Y-%m-%d')` is `2026-09-27`. `%s` is refused: the C library formats it as local time, off by the host's UTC offset; use `timestamp()` for Unix seconds |
| `timestamp()` | The current Unix time in whole seconds, as a number |
| `timestamp_ms()` | The current Unix time in whole milliseconds, as a number |

`timestamp()` and `timestamp_ms()` are numbers, and a header value must be
text: a header that is the call alone, `"X-Timestamp": "{{ timestamp() }}"`,
fails the stage ("Input should be a valid string"). Write
`"{{ str(timestamp()) }}"`, or put the call inside text
(`"t={{ timestamp() }}"`), which renders as text. A number is fine as it is in
`params` and in a JSON body.

Every call reads the clock, so two calls in one request can differ. To use one
moment in several places, bind it once in `vars`; an expiry an hour ahead is
`timestamp() + 3600`:

```json
{
    "substitutions": [{"vars": {"sent_at": "{{ now() }}", "expires": "{{ timestamp() + 3600 }}"}}],
    "stages": [
        {
            "name": "create_order",
            "request": {
                "url": "https://api.example.com/orders",
                "method": "POST",
                "headers": {"X-Sent-At": "{{ sent_at }}"},
                "body": {"json": {"placed_at": "{{ sent_at }}", "expires": "{{ expires }}"}}
            }
        }
    ]
}
```

#### Encoding

| Call | Returns |
| --- | --- |
| `b64encode(value, urlsafe=false)` | The base64 of `value`, padded: text is encoded as UTF-8 first, and bytes (from a fixture or function) are taken as they are. `urlsafe=true` (or `true` as the second argument) uses `-` and `_` for `+` and `/` |
| `b64decode_bytes(value, urlsafe=false)` | Decode base64 to bytes, including data that is not UTF-8 text. Padding is optional; `urlsafe=true` accepts `-` and `_` |
| `hex_bytes(text)` | Decode hexadecimal text to bytes; use this for a MessagePack binary field |
| `hexencode(bytes)` | Encode bytes as lowercase hexadecimal text |
| `msgpack_pack(value)` | Encode a value as MessagePack bytes, for a raw body or a percent-encoded query parameter |
| `b64decode(value, urlsafe=false)` | The text base64 `value` encodes, which must be UTF-8. Padding is optional; `urlsafe=true` reads the URL-safe alphabet |
| `json_dumps(value)` | `value` as JSON text, as Python's `json.dumps` writes it by default: `{"a": 1, "b": [1, 2]}`, with non-ASCII characters escaped. A `vars` object is written as the object it is |
| `json_loads(text)` | The JSON value `text` holds, objects as dicts |

Reading a claim from a JWT a login stage saved:

```json
{
    "headers": {"X-User": "{{ json_loads(b64decode(token.split('.')[1], urlsafe=true))['sub'] }}"}
}
```

For HTTP Basic authentication there is nothing to encode by hand: use the
built-in [`basic` auth](requests.md#authentication).

#### URLs

| Call | Returns |
| --- | --- |
| `urlencode(obj)` | A query string from an object, a `vars` object or a dict, encoded as `params` would send it: `{"q": "a b", "tag": ["x", "y"]}` gives `q=a+b&tag=x&tag=y`. A list repeats its key, `true` and `false` stay lowercase, and `null` sends an empty value. Bytes (from a fixture or function) are percent-encoded as they are. A value that is itself an object or a list of lists fails, and so does a function passed without calling it (`dict(at=now)`) |
| `quote(text, safe='')` | `text` percent-encoded (UTF-8) for one path segment: every reserved character is encoded, `/` included, unless it is listed in `safe` |

`params` is still the way to send a stage's own query; `urlencode` is for a
query inside a value, such as a callback URL passed as a parameter.
`quote` keeps a value from breaking the path it goes in:

```json
{
    "url": "https://api.example.com/files/{{ quote(file_name) }}",
    "params": {"return_to": "https://app.example.com/done?{{ urlencode(state) }}"}
}
```

#### Hashing

| Call | Returns |
| --- | --- |
| `sha256(value)` | The hex SHA-256 digest of `value` |
| `md5(value)` | The hex MD5 digest of `value`, for checksums such as `Content-MD5`, not for security |
| `hmac_sha256(key, message, encoding='hex')` | The HMAC-SHA256 of `message` under `key`, as hex, or as base64 with `'base64'` |

Text is encoded as UTF-8 first. A number fails rather than being turned into
text for you: `sha256(str(order_id))` says which text is hashed. Signing a
request, with the signed text sent as the body so that the two are the same
bytes:

```json
{
    "substitutions": [
        {"vars": {"sent_at": "{{ str(timestamp()) }}", "payload": {"order": 42, "qty": 1}}},
        {"vars": {"body_text": "{{ json_dumps(payload) }}"}}
    ],
    "request": {
        "url": "https://api.example.com/orders",
        "method": "POST",
        "headers": {
            "Content-Type": "application/json",
            "X-Timestamp": "{{ sent_at }}",
            "X-Signature": "{{ hmac_sha256(api_secret, sent_at + '.' + body_text) }}"
        },
        "body": {"text": "{{ body_text }}"}
    }
}
```

#### Errors

A helper given a value it cannot take fails the stage, naming the function and
the template:

```text
TypeError in expression '{{ sha256(order_id) }}': sha256() takes text or bytes, not int
ValueError in expression '{{ b64decode(token) }}': b64decode() got text that is not base64 (Only base64 data is allowed)
```

#### Your names come first

A name you define yourself, a variable, fixture, parameter, saved value or
function substitution, takes precedence over a built-in of the same name,
except that `get()` and `exists()` always call the built-in. In a scenario that
saves `timestamp`, `{{ timestamp }}` reads the saved value, and
`{{ timestamp() }}` still calls the built-in unless your `timestamp` is itself
a function: a fixture or a function substitution.

Your name wins only where it is in scope. Before a save lands, or outside the
stage whose `fixtures` list names it, the built-in of that name is what a
template gets: a read gets the function instead of your value, and a call runs
the built-in, silently. `validate` reads names the same way. A read out of
scope is reported as any name used out of scope (`HTTPCHAIN003`, `004` or, at
scenario level, the errors `016`/`017`), with a note on what the read gets:
the built-in in your value's place or, for a built-in that is no use as a
value (`now`, `timestamp`, `env`, ...), a function no template may render to,
which fails the stage (at scenario level, scenario initialization, or the
scenario's collection where a templated parametrize value resolves the
`substitutions` there). Handed to your own function, a saved value or variable
out of scope is such a read: `sign(timestamp)` ahead of the save gives `sign`
the built-in function. A use as a function cannot fail there: a call where
your fixture or function substitution is out of scope, or the name handed to a
function that takes one where your definition is, as a `key=` (`key=len`), or
to your own function or a method of your fixture's object where your
definition is a fixture or function substitution. Each is the warning
`HTTPCHAIN036`, at every level.
A stage that fakes the clock with `{"functions": {"timestamp": "clock:fixed"}}`
leaves every other stage, and the scenario-level `substitutions`, calling the
real `timestamp()`, and `validate` says so:
[filter](../diagnostics.md#filtering-collection-warnings) `HTTPCHAIN036` if
that is what you mean, or give your function a name of its own. `show` and
`graph` list a save named like a built-in as consumed where a template reads
it.

### List/Dict Comprehensions

An object in a list reads by attribute (`item.id`) or by key (`item['id']`),
whether it comes from scenario `vars`, a save or a `combinations` parameter, or
is a dict a fixture returned (see [Reading objects](#reading-objects)). An
object of a fixture's own type reads as that type allows: a `SimpleNamespace`
by attribute only.

```json
{
    "body": {
        "json": {
            "ids": "{{ [item.id for item in items] }}",
            "names": "{{ {item.id: item.name for item in items} }}"
        }
    }
}
```

Note: Comprehension length is limited by `httpchain_max_comprehension_length` config.

### Templates that render to `null`

A template can render to `null`: `get()` without a default, or a JMESPath save
of a key the response did not have. On an optional setting or check, `null`
reads as "not declared", so instead of quietly switching it off the stage
fails, naming the field and the template as written:

```
'verify.headers.Content-Type.contains' was declared as '{{ expected_ct }}' but rendered to None, which would silently disable it
```

This covers every optional field that takes a template: `verify.status`,
`verify.body.schema`, the header matcher fields (`contains`, `not_contains`,
`matches`, `not_matches`), `parallel.calls_per_sec`, each of
[`parallel.thresholds`](../advanced/parallel.md#thresholds) (`min_success_ratio`,
`max_mean_ms`, `max_p50_ms`, `max_p95_ms`, `max_p99_ms`, `min_rps`),
`retry.max_delay`, `request.auth`, and
scenario-level `ssl.cert` and `client.base_url`, `client.proxy`,
`client.max_connections` and `client.max_keepalive_connections` (which fail
scenario initialization: the first stage fails, and every later stage skips). A header matcher written as one template
(`"Content-Type": "{{ matcher }}"`) is covered too: a key the rendered matcher
sets to `null` fails, naming the field and that template, while a key it leaves
out is simply not checked.

So is the `group` of a [`save.regex`](responses.md#regex-extraction) entry.
Left out, it does not switch the save off but picks the default group (group 1,
or the whole match), so the message says that instead:

```
'save.regex.token.group' was declared as '{{ which }}' but rendered to None, which would silently save the default group instead
```

So are the `filename` and `content_type` of a
[multipart file](requests.md#file-uploads-multipart). Left out, they are sent
as the default ones (the path's name, the content type its extension
suggests), so the message says that:

```
'request.body.multipart.files.photo.filename' was declared as '{{ name }}' but rendered to None, which would silently send the default filename instead
```

A file object one template renders (`"photo": "{{ upload }}"`) is covered as a
header matcher written as one is, but for its sources: a file sets exactly one
of `path`, `content` and `base64`, so a `null` in the others is not set, as in
the same object written out, and the file is sent from the one it sets.

So are the operands of a [`verify.jmespath`](responses.md#jmespath-assertions)
matcher, `eq`, `ne`, `contains` and `not_contains` included, although `null` is
an operand they take. There the `null` would not switch the check off but
compare with `null` in place of the value the template was written for, so
the message says how to mean it instead:

```
'verify.jmespath["data.id"].eq' was declared as '{{ user_id }}' but rendered to None; to compare with null, write null
```

A `verify.jmespath` value is not a field. Its template that renders to `null`
compares with `null`, and one that renders an object is compared as that
object, even one shaped like a matcher (`{"ne": null}`): a matcher is written
as an object, never rendered whole.

A required field such as `url`, `timeout` or a built-in auth's credential
(`request.auth.bearer`, `auth.basic.password`), the `pattern` and `all` of a
`save.regex` entry written as an object, and a header matcher whose only
field rendered to `null`, cannot be switched off, but they fail with the same
message, naming the field and the template; it just ends at "rendered to None".
A bearer token that rendered to `null` does not send the request without
credentials, nor with the scenario's.
An exact-match header string is a value in the `headers` map rather than a
field, and fails validation when its template renders to `null`; so does an
entry of a `verify.status` list (`["{{ created_status }}", 409]`), and a
`save.regex` pattern written as a string. A stage's
[`skip_if`](scenarios.md#skipping-a-stage-at-runtime) and a verify expression
must evaluate to a boolean, so one that renders to `null` fails the stage
(`skip_if must evaluate to bool, got None from ...`) instead of
being read as false.

If something else in the same request or setting, or the same verify check
(a header matcher, a `jmespath` entry), is invalid too, pydantic's report on it
follows the message, so the `null` is never blamed for an error it did not
cause. A verify step's other checks still run, and their failures are listed
with it (see [Verify Steps](responses.md#verify-steps)).

Where `null` is itself a value, it is passed on as one:

-   a JSON body (`"json": "{{ payload }}"`) sends the JSON document `null`;
-   a `verify.jmespath` value (`"deleted_at": "{{ gone }}"`) is compared with
    `null`;
-   a query parameter or form field is sent with an empty value (`?q=`);
-   an auth function kwarg or a substitution variable receives `None`;
-   `always_run` is evaluated for truthiness, so `null` means "do not run".

## Stage-Level Substitutions

Override or add variables for specific stages:

```json
{
    "substitutions": [
        {"vars": {"base_url": "https://api.example.com"}}
    ],
    "stages": [
        {
            "name": "production_test",
            "substitutions": [
                {"vars": {"base_url": "https://prod.example.com"}}
            ],
            "request": {
                "url": "{{ base_url }}/health"
            }
        }
    ]
}
```

## Using Saved Values

Values saved from responses are available in subsequent stages:

```json
{
    "stages": [
        {
            "name": "login",
            "request": {
                "url": "https://api.example.com/login",
                "method": "POST",
                "body": {"json": {"user": "test", "pass": "secret"}}
            },
            "response": [
                {
                    "save": {
                        "jmespath": {
                            "auth_token": "token",
                            "user_id": "user.id"
                        }
                    }
                }
            ]
        },
        {
            "name": "get_profile",
            "request": {
                "url": "https://api.example.com/users/{{ user_id }}",
                "headers": {
                    "Authorization": "Bearer {{ auth_token }}"
                }
            }
        }
    ]
}
```

## Fixtures in Context

Pytest fixtures are added to context when listed:

```python
# conftest.py
import pytest
import os


@pytest.fixture
def api_key():
    return os.environ.get("API_KEY", "test-key")


@pytest.fixture
def test_user():
    return {"id": 1, "name": "Test User"}
```

```json
{
    "fixtures": ["api_key", "test_user"],
    "stages": [
        {
            "name": "authenticated_request",
            "request": {
                "url": "https://api.example.com/data",
                "headers": {
                    "X-API-Key": "{{ api_key }}"
                },
                "body": {
                    "json": {
                        "user_id": "{{ test_user['id'] }}",
                        "user_name": "{{ test_user['name'] }}"
                    }
                }
            }
        }
    ]
}
```
