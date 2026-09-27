# pytest_httpchain.models

Pydantic models for pytest-httpchain HTTP test scenarios.

## Purpose

This subpackage provides strongly-typed Pydantic models for defining HTTP test scenarios, including:
- Request/response structure validation
- Multiple body types (JSON, XML, Form, GraphQL, etc.)
- Variable substitutions and user functions
- Response verification and data extraction
- Test parameterization and parallel execution

## Public API

`parametrize_values_contain_template(parametrize)` — True when any parametrize
VALUE (not `ids`) contains a `{{ }}` template. Single source of truth shared by
the factory (decides whether scenario substitutions must resolve at collection
time) and the validator (reports that as the `HTTPCHAIN025` info diagnostic).

`is_relative_url(url)` — True when a URL has no scheme, i.e. it is a relative
reference that the scenario's `client.base_url` completes. Shared by the
request-URL type (`HttpUrlReferenceStr`, which checks an absolute and a relative
URL differently), the request builder (fails a stage whose rendered URL is
relative without a base_url) and the validator (`HTTPCHAIN034`).

`is_status_class(value)` — True for a status class (`"2xx"`), the pattern the
`StatusClass` type validates. `verify.status` holds codes, classes (kept
lowercase), a list of them, or template text a template rendered to, which the
field's template branch accepts; `response_steps` tells them apart with it and
refuses the last.

## URL types

URLs are validated but passed to httpx as written, never normalized:
`HttpUrlReferenceStr` (a request URL: absolute http(s), or relative),
`BaseUrlStr` (`client.base_url`: absolute, no query or fragment) and
`ProxyUrlStr` (`client.proxy`: http, https, socks5 or socks5h). Each refuses a
string containing a `{{ }}` so the `PartialTemplateStr` branch beside it takes
it, and refuses what httpx would send differently from what is written.

`BaseUrlStr` and `ProxyUrlStr` never quote the URL they refuse, and their
template branch is `UnquotedPartialTemplateStr`, which does not either: the
userinfo is credentials, usually rendered from the environment when the
scenario initializes. `ClientConfig` sets `hide_input_in_errors` for the same
reason, which keeps pydantic's `input_value` out of its errors.

Only the outermost validator's `hide_input_in_errors` counts, so it sits on
each one the carrier validates on its own once rendered and that can carry a
credential: `ClientConfig`, `Request` (a request's auth and headers) and the
adapter behind `validate_rendered_scenario_auth` (a scenario's `auth`).

`hide_input_in_errors` keeps out only pydantic's `input_value`, not a
validator's own message. A whole `auth` written as one template renders a
token as readily as a function name (`"auth": "{{ token }}"`), and the name's
validators quote what they refuse, so both auth unions refuse a string that is
neither a `module:function` name nor a template ahead of their members, in one
unquoted message (`_refuse_auth_string_unquoted`). A rendered string that is a
valid name passes, and basic credentials written as one (`"user:password"`)
are: the model cannot tell them apart, so `request_builder.build_auth`, given
the auth as declared, does not quote a user function name a template rendered
when importing or calling it fails (`userfunc.import_function(quoted=False)`).

## Common Patterns

### Discriminated Unions
Body types, substitutions, and save types use Pydantic discriminators based on field presence for automatic type detection.

`auth` does too: a string is a user function's `module:function` name, an object is told by its key (`name` for a user function with kwargs, `basic`, `digest` or `bearer` for a built-in), and several keys are resolved as everywhere (the first by name wins, the rest are its extras). `_create_discriminator`'s `value_to_tag` maps `False` (by identity: not `0`, not `true`) to the `false` tag, which only a request's union (`RequestAuth`) has; a scenario's (`ScenarioAuth`) refuses `false` ahead of it with a message saying where it belongs.

A scenario's `auth` is rendered on its own, not as part of `Scenario`, so the carrier re-validates it with `validate_rendered_scenario_auth` against the whole union: declared as one template, it is a `UserFunctionName` until it renders, and may render any member (`"{{ creds }}"` a built-in). A request's is re-validated as part of `Request`, so it gets the union anyway.

### Strict Validation
All models derive from `StrictModel` (`extra="forbid"` + a before-validator): unknown keys are rejected, so typos fail at validation instead of silently changing behavior. The one exception is `"$schema"` (editor metadata), which `StrictModel` drops from any dict a model consumes; `"$schema"` inside plain dict *values* (e.g. an inline response-body JSON Schema) is preserved.

### Input normalization
Substitutions, Responses, and Stages accept either a list OR a name-keyed mapping. `normalize_list_input` flattens a dict's values into a list (list values are extended in, scalars are appended); it is exported because `scoping` indexes raw-JSON entries positionally against the validated models and must flatten them identically. `_normalize_stages_input` turns a dict into a list where each KEY overrides/becomes the stage's `name` field.

### Namespace conversion
`VarsSubstitution.vars` values are converted dict->`SimpleNamespace` (so `{{ var.attr }}` attribute access works in templates). `JsonBody.json` and GraphQL `variables` convert `SimpleNamespace`->dict for JSON serialization (`NamespaceOrDict`), and so do `ResponseBody.schema` (a schema is plain JSON) and each `Verify.headers` value (a namespace there is a `HeaderMatcher`): a whole-value template over `vars` renders a namespace, which walk()'s re-validation would otherwise refuse. The last two wrap their field's whole union, so its member tags in pydantic's error locations stay as they were. So does `auth`, but only for a namespace at its top (`"auth": "{{ creds }}"`), which it converts all the way down, a user function's kwargs included, as the object was written; inside an auth written as an object a namespace is one rendered value, which a user function's kwargs hand on as it is and a built-in's credentials take as the object. `CombinationsParameter.combinations` converts the `SimpleNamespace` items of any sequence pydantic takes as its list (a rendered tuple too) to dicts, one level only: a template over `vars` renders the combinations as namespaces, which stand for the dicts, while the values inside keep their attribute access. Stage `parametrize` (factory) and `parallel.foreach` (carrier) both get it from re-validating the rendered value.

### Two-phase validation
Models validate twice. First with `{{ }}` template strings treated as opaque (`TemplateExpression` / `PartialTemplateStr` / `Any`), then again after the templates engine renders them at runtime, just before consumption — so the rendered concrete value is validated against the real type.
