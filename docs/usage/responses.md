# Response Processing

Response processing is defined as a list of steps, each either a `verify` or `save` operation. Steps execute in order.

## Response Structure

```json
{
    "response": [
        {"verify": {...}},
        {"save": {...}},
        {"verify": {...}}
    ]
}
```

Or using dictionary format for organization:

```json
{
    "response": {
        "status_check": {"verify": {"status": 200}},
        "extract_data": {"save": {"jmespath": {"id": "data.id"}}},
        "validate_schema": {"verify": {"body": {"schema": {...}}}}
    }
}
```

## Verify Steps

A verify step holds any mix of the checks below. They all run, in this order,
and the step fails with every one that failed, so that one run shows all that
is wrong with a response:

1. `status`
2. `headers`
3. `jmespath`
4. `expressions`
5. `user_functions`
6. `body.schema`
7. `body.contains`, `body.not_contains`, `body.matches`, `body.not_matches`

Headers, `jmespath` entries, expressions, user functions and `body` operands
run in the order they are written, and each is a check of its own. So is each
field a header matcher sets, run as `contains`, `not_contains`, `matches`,
`not_matches`, and each key a `jmespath` matcher sets, run in the order the
[matcher table](#jmespath-assertions) lists them, whatever order they are
written in. One failure reads as its own message. Several are counted, then
numbered in the order the checks ran:

```text
3 verification checks failed:
  1. Status code doesn't match: expected 201, got 200
  2. JMESPath 'data.id' doesn't match: expected 42, got 43
  3. Body doesn't contain 'created'
```

A check that cannot run fails once. A body that is not JSON is one failure
for the step, however many `jmespath` entries and `body.schema` wanted it; a
`jmespath` expression that cannot be evaluated against the body is one, not
one per key of its matcher; and `body.schema` is one check, reporting the
first violation it finds. A user function that raises is one failure, with
its error; in a list of several, a function's failure names it with its index,
so that two calls of one function can be told apart:
`Function 'mymodule:check_with_args' (user_functions[1]) verification failed`.

The step's templates all render before its first check runs, each check's on
its own, so a template that fails is one failure, listed in its check's place,
and only its own check does not run: an expression that raises (`KeyError` on
`response.headers['x-missing']`), or a value that
[renders to `null`](substitutions.md#templates-that-render-to-null). For a
header or a `jmespath` entry, the check is the whole entry, matcher and all.

A user function that calls `pytest.skip()`, `pytest.xfail()` or `pytest.fail()`
ends the step there, as it ends the stage: the checks after it do not run. It
cannot undo a failure, though: if a check before it failed, or a template of the
step did not render or called `pytest.fail()`, the stage fails with those
failures (a `pytest.fail()` message listed among them, in its place) rather
than being skipped or xfailed. A function a template calls is taken the same
way, except that the templates after that one are not rendered.

**Steps run in order, and a step that fails ends the stage.** The steps after
it do not run, since they may depend on it: a `save` a later `verify` uses, a
check that assumes an earlier one held. A `save` step stops at its first
error. So put checks in one verify step to see all their failures together,
and in separate steps to stop at one, so that nothing after it runs once it
fails.

### Status Code

```json
{
    "verify": {
        "status": 200
    }
}
```

Any integer from 100 to 599 is accepted, including nonstandard codes such as
nginx's `499`.

A **status class** matches every code with the same first digit: `"2xx"` passes
any 200-299 response. The classes are `"1xx"` to `"5xx"`, written in either
case (`"2XX"`).

```json
{
    "verify": {
        "status": "2xx"
    }
}
```

A **list** of codes and classes passes when the response matches any one of
them. It must not be empty.

```json
{
    "verify": {
        "status": ["2xx", 304]
    }
}
```

Because a longer list accepts more, a `$merge`/`$include` sibling never
concatenates its `status` list onto a fragment's, as it does other lists: an
equal list is kept and a different one is a merge conflict, so a shared
fragment's status check cannot be silently widened (see
[Status lists merge whole](../advanced/ref-merging.md#status-lists-merge-whole)).

On a mismatch, the failure names what was expected in the form it was written:

```
Status code doesn't match: expected 200, got 500
Status code doesn't match: expected 2xx, got 500
Status code doesn't match: expected one of [200, 201], got 500
```

Use template expressions:

```json
{
    "verify": {
        "status": "{{ expected_status }}"
    }
}
```

A template may render to any of these forms: a code (`201`, or the text
`"201"`), a class (`"2xx"`) or a list (`[200, 201]`). A list entry can be a
template of its own, rendering to a code or a class:

```json
{
    "verify": {
        "status": ["{{ created_status }}", 409]
    }
}
```

A `status` template that renders to `null` fails the stage rather than skipping
the check (see [Templates that render to `null`](substitutions.md#templates-that-render-to-null)),
and so does a list entry that renders to `null`, an empty list, or anything
else that is not a code or a class, such as text that is itself a template
(`"{{ ... }}"`).

### Headers

A **string** value is matched by exact, full-string equality — not substring. A
response with `Content-Type: application/json; charset=utf-8` does **not** match
`"application/json"`.

```json
{
    "verify": {
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "X-Request-Id": "{{ request_id }}"
        }
    }
}
```

For partial or pattern matches, use a **matcher object** instead of a string.
Any combination of `contains`, `not_contains`, `matches`, `not_matches`
(regexes, checked with `re.search`) is allowed; at least one is required:

```json
{
    "verify": {
        "headers": {
            "Content-Type": {"contains": "application/json"},
            "X-Request-Id": {"matches": "^[0-9a-f-]+$"},
            "Warning": {"not_contains": "deprecated"}
        }
    }
}
```

A matcher can also be one template, such as `"Content-Type": "{{ ct }}"`, with
`ct` an object in scenario `vars` (`{"contains": "json"}`) or saved from the
response.

An **absent** header behaves as an empty string for matcher forms — `contains`
and `matches` fail, `not_contains` and `not_matches` pass vacuously. (Exact
string form fails for an absent header, as before.)

A failed check on a header whose values are redacted (`Set-Cookie`,
`Authorization`, ... — see [Secrets in reports](../getting-started.md#secrets-in-reports))
shows its value redacted, and so does the expected value of an exact match:
`Header 'Set-Cookie' doesn't match: expected session=[REDACTED]; Path=/, got session=[REDACTED]; Path=/admin`.
A failed `not_contains` or `not_matches` found its operand in the value, so the
message shows the operand only when the redacted value already shows its text,
and `[REDACTED]` otherwise: a logout check's `"not_contains": "{{ old_session }}"`
fails with `... contains '[REDACTED]' while it shouldn't`. A failed `contains` or
`matches` operand is not in the value and is shown as written.

A matcher field whose template renders to `null` fails the stage, naming the
field and the template, even when another field of the same matcher still holds
a check: it is never quietly skipped. The same holds for a matcher written as
one template, such as `"Content-Type": "{{ matcher }}"` with `matcher` saved
from the response: a key it sets to `null` fails, and only a key it leaves out
goes unchecked.

### JMESPath Assertions

`jmespath` asserts on the JSON response body directly. Each key is a
[JMESPath](https://jmespath.org) expression, and its value is what the
expression must give: a value to equal, or a matcher object.

```json
{
    "verify": {
        "jmespath": {
            "data.id": "{{ user_id }}",
            "length(items)": 3,
            "items[0].name": {"matches": "^A"},
            "meta": {"eq": {"page": 1}},
            "price": {"gt": 0, "lt": 100},
            "tags": {"contains": "new", "length": 2},
            "deleted_at": null
        }
    }
}
```

A **value** that is not an object (a string, number, boolean, `null` or array)
must equal what the expression gives, by JSON equality: a boolean never equals
a number (`true` is not `1`), an integer equals the same number written with a
fraction (`1` is `1.0`), and arrays and objects are compared element by
element with the same rules. Text is not a number either: `"42"` does not equal
`42`.

An **object** is always a matcher, and every key it sets must hold:

| Key | Holds when the value... |
|-----|-------------------------|
| `eq` | equals the operand (JSON equality, as above) |
| `ne` | does not equal the operand |
| `gt`, `ge`, `lt`, `le` | is a number `>`, `>=`, `<`, `<=` the operand, which is a number too |
| `contains` | is a string holding the operand as a substring, an array holding an element equal to it, or an object holding it as a key |
| `not_contains` | is a string, array or object that does not |
| `matches` | is a string the operand, a regex, matches (`re.search`) |
| `not_matches` | is a string the regex does not match |
| `type` | is of that JSON type: `string`, `number`, `integer`, `boolean`, `array`, `object` or `null` |
| `length` | is a string, array or object of that many characters, elements or keys |

`integer` is a number written without a fraction or exponent (`1`, not `1.0`
or `1e0`), `number` is any number, and neither is a boolean. A value a key
cannot judge fails the check rather than passing it: `gt` on a string,
`matches` on a number, `contains` on `null`.

To compare with an object, give it as `eq`: `"meta": {"eq": {"page": 1}}`. A
literal object in its place is a matcher with unknown keys, and fails
validation saying so:

```
An object here is a matcher, and 'page' is not one of its keys (eq, ne, gt, ge, lt, le,
contains, not_contains, matches, not_matches, type, length); to compare with an object,
give it as eq: {"eq": {...}}
```

A literal object whose keys all happen to be a matcher's is read as that
matcher. When its values are no operands (`"role": {"type": "admin"}`), it
fails validation with the matcher's own errors and the same hint beside them.
When they are (`{"type": "string"}`, a JSON Schema fragment), it is a valid
matcher and checks as one, so an object like that must be given as `eq` to be
compared: `{"eq": {"type": "string"}}`.

A failure names the expression, what was expected and what the body held,
the last cut short when it is long:

```
JMESPath 'data.id' doesn't match: expected 42, got 43
JMESPath 'price' doesn't match: expected lt 100, got 120.5
JMESPath 'tags' doesn't match: expected length 2, got ["new"] (length 1)
JMESPath 'name': gt needs a number, got "Alice" (string)
```

The body is parsed once per verify step, by the first check that reads it,
`jmespath` or `body.schema`; one that is not JSON fails the stage, once for
the step whatever else wanted it
(`Cannot check verify.jmespath, response is not valid JSON: ...`), and so does
one nested too deeply for Python's parser. So does an expression that cannot
be evaluated against the body, naming why: a function given a value it does
not take (`JMESPath 'keys(items)' cannot be evaluated against the response
body: keys() needs object, got [1, 2] (array)`) or an array holding one
(`join() needs array-string, got an array holding 1 (number)`), `ceil()` of a
number too large for Python (`1e400`), or a function JMESPath does not have or
a wrong number of arguments, which it finds only when it calls the function,
so `validate` does not.

**Templates.** Values and matcher operands take templates, rendered like every
other verify field's: against the context, the `response` namespace and what
earlier steps saved. Keys are JMESPath and are never rendered, so a template
in one is caught: `"data.{{ field }}"` is not valid JMESPath and fails
validation saying a key cannot hold a template, and one that still is (a
quoted string such as `"'{{ x }}'"`, evaluated as written) gets the
`HTTPCHAIN029` warning. Put the template in the value the key maps to.

What is written decides what a template renders, not what it renders to. A
template written where a value goes renders the value to compare with, an
object included, whatever its keys: `"meta": "{{ saved_meta }}"` passes only
when `meta` equals the object saved earlier, by JSON equality. A matcher is
written as an object, and its operands take templates
(`"price": {"gt": "{{ low }}", "lt": "{{ high }}"}`); unlike a
[header matcher](#headers), it cannot come whole from one
template. A header's expected value is a string, so an object rendered there
can only be a matcher; a JMESPath value can be any JSON, an object too.

A value template that renders to `null` is compared with `null`, since `null`
is a value there. A matcher operand's template that renders to `null` fails
the stage instead, naming the operand and the template, as other checks do
(see [Templates that render to `null`](substitutions.md#templates-that-render-to-null)):
it most likely lost the value it was written for. To compare with `null`,
write `null`.

**Missing paths.** JMESPath gives `null` for a path that is not there, so
`null`, `{"eq": null}` and `{"type": "null"}` pass both for a missing key and
for a key holding `null`, and `{"ne": null}` fails both. Where the difference
matters, ask the object holding the key: `contains` on an object tests its
keys.

```json
{
    "verify": {
        "jmespath": {
            "data": {"contains": "deleted_at"},
            "data.deleted_at": null
        }
    }
}
```

passes only when `deleted_at` is there and `null`; `{"not_contains":
"deleted_at"}` passes only when it is missing.

### Expression Verification

Evaluate template expressions, each of which must evaluate to a boolean. Expressions are evaluated against the **context** — saved variables, fixtures, and substitutions — plus the reserved **`response` metadata namespace** (see below).

To check one value in the body, use [`jmespath`](#jmespath-assertions).
Expressions are for logic across values: two fields compared with each other,
a sum, a value against the response metadata. Save the body values the
expression needs first, then reference them; the saved names stay in the
context for later steps and stages:

```json
{
    "response": [
        {
            "save": {
                "jmespath": {
                    "total": "total",
                    "prices": "items[*].price"
                }
            }
        },
        {
            "verify": {
                "expressions": [
                    "{{ total == sum(prices) }}"
                ]
            }
        }
    ]
}
```

#### The `response` Namespace

Every **response step** (save and verify alike) sees a reserved `response`
namespace holding the response's metadata:

| Name | Type | Meaning |
|------|------|---------|
| `response.status` | int | HTTP status code |
| `response.reason` | str | Reason phrase (`"OK"`, `"Not Found"`) |
| `response.headers` | mapping | Response headers, case-insensitive keys |
| `response.elapsed_ms` | float | Round-trip time in milliseconds |

Use it directly in verify expressions:

```json
{
    "verify": {
        "expressions": [
            "{{ response.status == 200 }}",
            "{{ 'json' in response.headers['content-type'] }}",
            "{{ response.elapsed_ms < 500 }}"
        ]
    }
}
```

Or save a header for later stages with a substitutions save:

```json
{
    "save": {
        "substitutions": [
            {"vars": {"request_id": "{{ response.headers['x-request-id'] }}"}}
        ]
    }
}
```

The name `response` is reserved inside response steps: a variable, save, or
fixture with that name is shadowed there (the validator warns with
`HTTPCHAIN027`). It is only in scope in response steps — referencing it in a
request template is an error. The response **body** is deliberately not in the
namespace; assert on body data with [`jmespath`](#jmespath-assertions), or
extract it with a `save` step.

Response facets beyond the metadata — e.g. the raw body text — can be captured with a [save user function](#user-function-save), which receives the `httpx.Response`:

```python
# checks.py
import httpx


def body_meta(response: httpx.Response) -> dict:
    return {
        "body_text": response.text,
    }
```

```json
{
    "response": [
        {"save": {"user_functions": ["checks:body_meta"]}},
        {
            "verify": {
                "expressions": [
                    "{{ 'error' not in body_text }}"
                ]
            }
        }
    ]
}
```

`validate` can't see the keys a user function returns, so it reports `body_text` as `HTTPCHAIN003` ("potentially undefined"). The warning is expected here — the expression resolves correctly at runtime once the save step has run.

### Body Content Checks

#### Contains / Not Contains

```json
{
    "verify": {
        "body": {
            "contains": ["success", "user_id"],
            "not_contains": ["error", "failed"]
        }
    }
}
```

#### Regex Matching

```json
{
    "verify": {
        "body": {
            "matches": ["\"id\":\\s*\\d+", "\"status\":\\s*\"ok\""],
            "not_matches": ["\"error\":", "\"failed\":"]
        }
    }
}
```

### JSON Schema Validation

Inline schema:

```json
{
    "verify": {
        "body": {
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                    "email": {"type": "string", "format": "email"}
                },
                "required": ["id", "name"]
            }
        }
    }
}
```

An inline schema is **standard JSON Schema, verbatim**: the scenario's
reference resolver treats the whole `schema` value as opaque, so `$ref`,
`$defs`, `$schema` and every other keyword inside it are addressed to the
schema validator, exactly as in a standalone schema document. Scenario
reference directives are **not** processed inside an inline schema — the
validator flags `$include`/`$merge` (and a file-path `$ref`, which the
runtime schema validator can never resolve) with the `HTTPCHAIN028` warning,
and an unresolvable schema-internal `$ref` fails the stage with a clean
verification error. To share a schema between scenarios, reference it by
file path instead:

External schema file:

```json
{
    "verify": {
        "body": {
            "schema": "./schemas/user.json"
        }
    }
}
```

A relative schema path is resolved against the **scenario file's directory** —
the same rule as `$ref`/`$include` — so `"./schemas/user.json"` looks for
`schemas/user.json` next to the test file, regardless of where pytest was
launched from. The same rule applies to every file path in the dialect:
`body.binary`, `body.files` values, and `ssl.cert`/`ssl.verify`. Absolute paths
pass through unchanged.

The `schema` can also be one template, rendering a path or the schema itself:
`"schema": "{{ user_schema }}"`, with `user_schema` a schema object in scenario
`vars` or saved from an earlier response. Only an inline `schema` is opaque to
the scenario's reference resolver, though: a `$ref` in `vars` is the resolver's
to follow, so a schema with `$ref`s of its own belongs inline or in a file.

Either way, `format` is **checked**, not just recorded: a value that does not
conform to its `format` fails the stage (`'not-an-email' is not a 'email'`).
Which formats can be checked depends on the installed `jsonschema`. Out of the
box, with the default Draft 2020-12 dialect, those are `email` and `idn-email`
(both only require an `@`), `ipv4`, `ipv6`, `date`, `uuid`, `regex` and
`idn-hostname` (its `idna` dependency comes with httpx). `regex` is checked
against Python `re` syntax, not the ECMA-262 syntax JSON Schema specifies, so
JavaScript-only constructs such as `(?<year>\d{4})` named groups or `\p{Lu}`
property escapes fail it, and so does a pattern too large for `re` to compile
(`a{4294967296}`). The others — among them `date-time`, `time`, `hostname`,
`uri`, `uri-reference`, `iri`, `iri-reference`, `uri-template`,
`json-pointer`, `relative-json-pointer` and `duration` — need jsonschema's
optional format dependencies, and until those are installed any value passes
them. To check them, install `jsonschema[format-nongpl]` (or
`jsonschema[format]`, which uses the GPL-licensed `rfc3987` for `uri`/`iri`)
next to pytest-httpchain. A format name the schema's dialect does not define
(`uuid` under Draft 7, for example) is never checked.

### User Function Verification

```json
{
    "verify": {
        "user_functions": [
            "mymodule:check_response",
            {
                "name": "mymodule:check_with_args",
                "kwargs": {
                    "expected_value": "{{ expected }}"
                }
            }
        ]
    }
}
```

```python
# mymodule.py
import httpx


def check_response(response: httpx.Response) -> bool:
    data = response.json()
    return data.get("status") == "ok"


def check_with_args(response: httpx.Response, expected_value: str) -> bool:
    return response.json().get("value") == expected_value
```

A function returns `True` for a response that passes and `False` for one that
does not; one that raises fails its check with its error. It runs whatever the
checks before it in the step found, since a failed check no longer stops the
step, so it must not assume they passed: against an HTML error page that
failed `status`, `response.json()` above raises, and the step lists that error
as a second failure beside the status mismatch. A function that only makes
sense on a good response can check for one first and return `False`, or be
put in a verify step of its own after the one checking `status`, which it then
runs only once that step passed:

```python
def check_response(response: httpx.Response) -> bool:
    if not response.is_success or "json" not in response.headers.get("content-type", ""):
        return False
    return response.json().get("status") == "ok"
```

## Save Steps

### JMESPath Extraction

Extract values from JSON responses:

```json
{
    "save": {
        "jmespath": {
            "user_id": "data.user.id",
            "user_name": "data.user.name",
            "first_item": "items[0]",
            "all_ids": "items[*].id"
        }
    }
}
```

### Substitutions Save

Add computed values to context:

```json
{
    "save": {
        "substitutions": [
            {
                "vars": {
                    "timestamp": "{{ str(now_utc) }}"
                }
            },
            {
                "functions": {
                    "computed": "mymodule:compute_value"
                }
            }
        ]
    }
}
```

The template evaluator does not expose `datetime`, so provide values like timestamps via a fixture and reference the stage with `fixtures: ["now_utc"]`:

```python
# conftest.py
import pytest
from datetime import datetime


@pytest.fixture
def now_utc():
    return datetime.now()
```

### User Function Save

Extract data using custom functions:

```json
{
    "save": {
        "user_functions": [
            "mymodule:extract_data",
            {
                "name": "mymodule:extract_with_args",
                "kwargs": {
                    "key": "specific_field"
                }
            }
        ]
    }
}
```

```python
# mymodule.py
import httpx
from typing import Any


def extract_data(response: httpx.Response) -> dict[str, Any]:
    data = response.json()
    return {"user_id": data["user"]["id"], "token": response.headers.get("X-Auth-Token")}


def extract_with_args(response: httpx.Response, key: str) -> dict[str, Any]:
    return {key: response.json().get(key)}
```

## Complete Example

```json
{
    "stages": [
        {
            "name": "create_user",
            "request": {
                "url": "https://api.example.com/users",
                "method": "POST",
                "body": {
                    "json": {"name": "Test User", "email": "test@example.com"}
                }
            },
            "response": [
                {
                    "verify": {
                        "status": 201,
                        "headers": {
                            "Content-Type": "application/json; charset=utf-8"
                        },
                        "jmespath": {
                            "name": "Test User",
                            "id": {"type": "integer"},
                            "created_at": {"ne": null}
                        }
                    }
                },
                {
                    "save": {
                        "jmespath": {
                            "user_id": "id",
                            "created_at": "created_at"
                        }
                    }
                },
                {
                    "verify": {
                        "body": {
                            "schema": {
                                "type": "object",
                                "required": ["id", "name", "email"]
                            }
                        }
                    }
                }
            ]
        },
        {
            "name": "verify_user",
            "request": {
                "url": "https://api.example.com/users/{{ user_id }}"
            },
            "response": [
                {
                    "verify": {
                        "status": 200,
                        "jmespath": {
                            "id": "{{ user_id }}",
                            "created_at": "{{ created_at }}"
                        }
                    }
                }
            ]
        }
    ]
}
```
