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

## Common Patterns

### Discriminated Unions
Body types, substitutions, and save types use Pydantic discriminators based on field presence for automatic type detection.

### Strict Validation
All models derive from `StrictModel` (`extra="forbid"` + a before-validator): unknown keys are rejected, so typos fail at validation instead of silently changing behavior. The one exception is `"$schema"` (editor metadata), which `StrictModel` drops from any dict a model consumes; `"$schema"` inside plain dict *values* (e.g. an inline response-body JSON Schema) is preserved.

### Input normalization
Substitutions, Responses, and Stages accept either a list OR a name-keyed mapping. `normalize_list_input` flattens a dict's values into a list (list values are extended in, scalars are appended); it is exported because `scoping` indexes raw-JSON entries positionally against the validated models and must flatten them identically. `_normalize_stages_input` turns a dict into a list where each KEY overrides/becomes the stage's `name` field.

### Namespace conversion
`VarsSubstitution.vars` values are converted dict->`SimpleNamespace` (so `{{ var.attr }}` attribute access works in templates). `JsonBody.json` and GraphQL `variables` convert `SimpleNamespace`->dict for JSON serialization (`NamespaceOrDict`), and so do `ResponseBody.schema` (a schema is plain JSON) and each `Verify.headers` value (a namespace there is a `HeaderMatcher`): a whole-value template over `vars` renders a namespace, which walk()'s re-validation would otherwise refuse. The last two wrap their field's whole union, so its member tags in pydantic's error locations stay as they were. `CombinationsParameter.combinations` converts the `SimpleNamespace` items of any sequence pydantic takes as its list (a rendered tuple too) to dicts, one level only: a template over `vars` renders the combinations as namespaces, which stand for the dicts, while the values inside keep their attribute access. Stage `parametrize` (factory) and `parallel.foreach` (carrier) both get it from re-validating the rendered value.

### Two-phase validation
Models validate twice. First with `{{ }}` template strings treated as opaque (`TemplateExpression` / `PartialTemplateStr` / `Any`), then again after the templates engine renders them at runtime, just before consumption — so the rendered concrete value is validated against the real type.
