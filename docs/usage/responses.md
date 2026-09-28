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
schema validator, exactly as in a standalone schema document. A `$ref` is
resolved as JSON Schema resolves one: `#/$defs/item` within the schema, and a
local file (`common.json#/$defs/Email`) relative to the **scenario file's
directory**, as described [below](#references-between-documents). Scenario
reference directives are **not** processed inside an inline schema — the
validator flags `$include`/`$merge` there with the `HTTPCHAIN028` warning — and
a `$ref` that does not resolve fails the stage with a clean verification error.
To share a schema between scenarios, keep it in a file:

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
`body.binary`, the files of `body.files` and `body.multipart.files` (a path, or a
file object's `path`), and `ssl.cert`/`ssl.verify`. Absolute paths pass through
unchanged.

#### A schema inside a document: OpenAPI and shared schema files

A schema file path may end in a **JSON pointer**, which selects one schema
inside the document, so a response can be checked against the contract you
already have — an OpenAPI 3.1 document's components, or a file of shared
definitions:

```json
{
    "verify": {
        "status": 200,
        "body": {
            "schema": "./openapi.json#/components/schemas/User"
        }
    }
}
```

with `openapi.json` next to the scenario, and `common.json` next to it:

```json
{
    "openapi": "3.1.0",
    "info": {"title": "Users", "version": "1.0.0"},
    "paths": {},
    "components": {
        "schemas": {
            "User": {
                "type": "object",
                "required": ["id", "email", "role"],
                "properties": {
                    "id": {"type": "integer"},
                    "email": {"$ref": "common.json#/$defs/Email"},
                    "role": {"$ref": "#/components/schemas/Role"}
                }
            },
            "Role": {"enum": ["admin", "user"]}
        }
    }
}
```

```json
{
    "$defs": {
        "Email": {"type": "string", "format": "email"}
    }
}
```

The part after the first `#` is an [RFC 6901](https://www.rfc-editor.org/rfc/rfc6901)
JSON pointer, written as a URI fragment, as a `$ref`'s is: `/` separates the
keys, `~1` stands for a `/` inside a key and `~0` for a `~`, and
percent-encoding is decoded first (`%20` for a space). An array item is its
index, `0`, `1`, and so on. So a path's response schema is
`openapi.json#/paths/~1users~1{id}/get/responses/200/content/application~1json/schema`.
Without a pointer, or with an empty one (`schema.json#`), the whole file is the
schema. What follows the `#` must be a pointer: a plain name (`#User`), which
JSON Schema reads as an anchor, fails `validate` and collection, and so does an
`http://` or `https://` URL in place of the file, or any URI (`file:`, or a
scheme followed by `//`). The first `#` ends the path, so a file whose path
holds one cannot be named here; a path that merely starts like a URI stays one
with `./` in front (`./http:v1/user.json`), and a colon anywhere else is the
path's own (`schemas:v1/user.json`).

##### References between documents

The selected schema's `$ref`s are resolved against the **whole document** it
is in, so `#/components/schemas/Role` inside `User` finds `Role`. A reference
to another local file — `./address.json`, `common.json#/$defs/Email` — is
resolved relative to **the file the reference is written in**, not to the
scenario: `common.json` above is looked for next to `openapi.json`, wherever
the scenario is, and a reference inside `common.json` is relative to
`common.json` in turn. An inline schema's references to files are relative to
the scenario file's directory, the one inline schemas have. An `$id` sets
another base, as below. A `$dynamicRef` is resolved the same way.

An **`$id`** is the base of the references inside it, as JSON Schema
specifies: in the schema `body.schema` names, at its root, on the pointer's way
to it, and further in. So a bundled document, whose `$ref`s name the resources
embedded in it by their `$id`s, resolves as written:

```json
{
    "$id": "https://example.com/schemas/customer",
    "type": "object",
    "properties": {
        "address": {"$ref": "/schemas/address"}
    },
    "$defs": {
        "address": {"$id": "/schemas/address", "type": "object", "required": ["street"]}
    }
}
```

`/schemas/address` is `https://example.com/schemas/address` there, the
embedded resource, not a file. The same holds in an OpenAPI component, although
JSON Schema itself does not look into a `components/schemas` map: a component
that declares an `$id` is the base of its own `#/$defs/...`, and the `$id`s
inside it name their resources, whether the pointer selects the component or a
schema inside it (`#/components/schemas/User/properties/address`). A path's
response schema is a component in this sense too, where the pointer goes into
it (`.../application~1json/schema/items`). Inside such a component, `#/...` is
relative to the component, not to the OpenAPI document, so
`#/components/schemas/Role` there points to nothing: reach another component
by an `$id` of its own, or drop the `$id`.

An `$id` that cannot be read fails the stage whatever the response, as an
unresolvable reference naming it: one on the pointer's way that is not a
string (`"$id": 5`) or not a URI (`"http://[x"`), and one in the selected
schema that is not a URI (its meta-check refuses one that is not a string).
JSON Schema reads them before any value is checked.

A document a `$ref` reaches is resolved as jsonschema resolves one, against
**where it is**, whatever `$id` its root declares, so **references between
files are relative paths**. A reference by an `$id`'s full URI finds a schema
that declares it in the same document, not in another file.

The flip side, in the schema `body.schema` names: under a root `$id` that is a
URL, a relative reference names a document at that URL, not a file beside the
schema. `address.json` under `"$id": "https://example.com/schemas/user.json"`
is `https://example.com/schemas/address.json`, which is not fetched (see
below). Drop the `$id`, or make it relative (`"$id": "user.json"`), to reach
the file.

A reference that names a **local file**, one that resolves to a path rather
than to a resource an `$id` names, keeps the rules a scenario's own `$include`
path keeps (see
[path traversal limits](../advanced/ref-merging.md#security-path-traversal-limits)),
and one that breaks them fails the stage:

- it is a **relative path**: an absolute path (`/schemas/user.json`,
  `//host/share/user.json`, `C:/schemas/user.json`) or a `file:` URI is refused;
- it climbs at most `httpchain_ref_parent_traversal_depth` directories (`../`,
  3 by default), counted as written;
- the file it names lies **inside pytest's rootdir**, symlinks resolved.

The schema file named in `schema` is not held to these rules, as no scenario
file path is; its references are.

Nothing is read over the network: a reference to an `http://` or `https://`
document fails the stage (`remote references are not fetched`), and so does one
to a file on another host (a base an `$id` such as `file://host/share/` sets),
refused before the path is looked up, and one to anything else that is not a
local file (`urn:...`). Keep a local copy of a hosted schema instead.

##### Dialect

The selected schema is validated under its own `$schema` if it declares one,
else the document root's, else **Draft 2020-12**, which is also what a
`$schema` jsonschema does not know means (OpenAPI 3.1's
`https://spec.openapis.org/oas/3.1/dialect/base`, for instance). A schema a
`$ref` reaches follows the same rule: its own `$schema`, else the root
`$schema` of the document the reference points into, else the dialect of the
schema the `$ref` is in. So `common.json#/definitions/Pair` in a Draft 7 file
is validated as Draft 7 however it is reached, as `schema` or through a
`$ref`, and so is what its own references reach.

!!! warning "OpenAPI 3.0 schemas are not JSON Schema"
    OpenAPI 3.1 schema objects are JSON Schema 2020-12. OpenAPI **3.0**'s are
    a dialect of their own, close to Draft 4 but not the same: `nullable: true`
    is ignored (so `null` fails a `"type": "string"`), `exclusiveMinimum` and
    `exclusiveMaximum` are booleans (which Draft 2020-12 refuses, and
    `validate --deep` reports), and `example`, `discriminator` and `xml` are
    annotations. Convert a 3.0 document to 3.1 first — `nullable: true` beside
    `"type": "string"` becomes `"type": ["string", "null"]` — or declare
    `"$schema": "http://json-schema.org/draft-04/schema#"` at its root and
    avoid `nullable`.

##### Reading and failures

Each schema file is read and parsed once for as long as it is unchanged,
however many stages, iterations (a `parallel` stage's included, whose
iterations wait for the one that reads it) and references use it; a file a
stage rewrites is read again. The schema the pointer selects is checked against
its dialect's meta-schema, once too, and the `$id`s and anchors of its document
are indexed once. Each problem fails the stage as a verification error that
names the file, as the path it resolved to, and the pointer, listed with the
step's other failures:

- `Error reading body schema file '.../api/openapi.json#/components/schemas/User': ...`
  — the file is missing, or not JSON.
- `Body schema pointer '#/components/schemas/Usr' leads nowhere in file
  '.../api/openapi.json': '#/components/schemas' has no key 'Usr'`.
- `Invalid JSON Schema in file '.../api/openapi.json#/components/schemas/User': ...`
  — the selected schema fails its meta-schema.
- `Cannot resolve a reference in body schema file '.../api/openapi.json#/components/schemas/User':
  $ref 'common.json#/$defs/Email' names .../api/common.json, which does not exist`
  — and likewise for a remote reference, a file on another host, an absolute
  path, one that climbs too far or leads outside the rootdir, a file that is
  not JSON, a pointer to nothing in a document the reference reaches, or an
  `$id` that cannot be read (`$id 5 is not a string: an $id is a URI reference`).
- `Cannot validate against body schema file '...': $ref '#/components/schemas/Role'
  points to an invalid JSON Schema: ...` — only the selected schema is checked
  against its meta-schema before the response is validated. A schema a
  reference reaches is checked when validating against it breaks down on a
  value its meta-schema refuses (`"type": "strin"`, a `$ref` that is not a
  string, a document that is a list), and named by the nearest reference to
  it, in the words `validate --deep` uses. An invalid keyword that no response
  reaches goes unnoticed at runtime; `validate --deep` finds it.

[`validate --deep`](../cli.md#validate) checks all of this without a response:
that the file exists and is JSON, that the pointer resolves, that the selected
schema is valid, and that every `$ref` and `$dynamicRef` it reaches, through
every file, resolves under the same rules to a schema valid in its dialect —
`HTTPCHAIN020` for a file that is not there, `HTTPCHAIN021` for the rest. It
follows what the runtime follows: under Draft 3 to 7, whose `$ref` ignores
the keywords beside it, a reference beside a `$ref` is not reached. An
inline schema's templates are rendered before it is used, so a reference that
holds one there is left to the runtime; a file is read as it is, so a `{{ }}`
in a reference there is checked as the text it is, as the runtime resolves it.

The `schema` can also be one template, rendering a path or the schema itself:
`"schema": "{{ user_schema }}"`, with `user_schema` a schema object in scenario
`vars` or saved from an earlier response. Only an inline `schema` is opaque to
the scenario's reference resolver, though: a `$ref` in `vars` is the resolver's
to follow, so a schema with `$ref`s of its own belongs inline or in a file.

Whatever form the schema takes, `format` is **checked**, in every schema its
references reach too, not just recorded: a value that does not conform to its
`format` fails the stage (`'not-an-email' is not a 'email'`).
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

### Regex Extraction

For a body that is not JSON, such as an HTML page or plain text, a `regex` save
takes values out of its text with [Python regular
expressions](https://docs.python.org/3/library/re.html#regular-expression-syntax):
a CSRF token from a form, an id from a sentence.

```json
{
    "save": {
        "regex": {
            "csrf": "name=\"csrf\" value=\"([^\"]+)\"",
            "order_id": {"pattern": "Order #(?P<id>\\d+)", "group": "id"},
            "all_ids": {"pattern": "id=(\\d+)", "all": true}
        }
    }
}
```

Each key is the variable to save, and each value a pattern, searched for
anywhere in the body (`re.search`) as text, decoded as the response's charset
says. The variable is group 1 of the first match when the pattern has groups,
and the whole match when it has none: `csrf` above is the token, not the
`name="csrf" value="..."` around it. A named group counts in the numbering, and
`(?:...)` does not.

An object picks the group, or every match:

| Key       | Value                                                                                                         |
|-----------|---------------------------------------------------------------------------------------------------------------|
| `pattern` | The regular expression (required).                                                                            |
| `group`   | The group to save: its number, `0` for the whole match, or its name. Not set: as for a string above.         |
| `all`     | `true` saves a list of that group from every match, in order, and `[]` when nothing matches. Default `false`. |

A pattern that does not match fails the step, naming the variable and the
pattern:

```text
Error saving variable csrf: regex 'name="csrf" value="([^"]+)"' does not match the response body
```

With `all`, nothing matching is not an error but an empty list, which a later
`verify` can check (`"{{ len(all_ids) > 0 }}"`). A group that takes no part in
its match, one side of an alternation (`(pending)|Order #(\d+)`), saves
`null`, as Python's `re` has it.

The values are strings (a list of strings with `all`), compared as such
(`"{{ order_id == '1042' }}"`), or converted where a number is wanted
(`"{{ int(order_id) }}"`). Flags go in the pattern itself: `(?i)` ignores
case, `(?s)` lets `.` match a newline, which it does not by default (a match
spanning lines of HTML needs it), and `(?m)` makes `^` and `$` match at each
line. In JSON a backslash is written twice: `\\d+` is the
pattern `\d+`, and a `"` inside the pattern is `\"`.

A pattern may hold templates, rendered before the search in the response
step's scope: `"<a href=\"(/item/{{ item_id }})\">"`. What a template puts
there is part of the pattern, so a `.` or a `+` in a value is regex syntax, not
the character. `group` and `all` take a template too. A pattern that is not a
valid regular expression, or a `group` the pattern does not have, fails
`validate` and collection when the pattern is written out, and fails the stage
when a template renders it:

```text
regex 'Order #(?P<id>\d+)' has no group named 'order' (its named groups: 'id')
```

Because a pattern takes templates, a `{{` in one always opens a template,
closed by the first `}}`, as in any value (a header or body `matches` pattern
too). Braces the body holds, such as a page's own `{{ name }}` placeholders,
are matched escaped: `"\\{\\{\\s*(\\w+)\\s*\\}\\}"`. A repeat count taken from
a template keeps its braces inside the expression: with `n` at 3,
`"\\d{{ '{' + str(n) + '}' }}"` renders `\d{3}`, while `"\\d{{{ n }}}"` is not a
valid template, and `"\\d{ {{ n }} }"` renders `\d{ 3 }`, which Python's `re`
reads as a digit followed by the text `{ 3 }`, not as a count.

The saved names are known to `validate`, which reports a template reading one
before the step that saves it, and to [`show` and `graph`](../cli.md), which
list them among the stage's saves.

### Substitutions Save

Add computed values to context:

```json
{
    "save": {
        "substitutions": [
            {
                "vars": {
                    "received_at": "{{ now() }}"
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

`now()` is one of the template [built-in functions](substitutions.md#built-in-functions),
with `timestamp()` and helpers for base64, JSON, URLs and hashes.

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
