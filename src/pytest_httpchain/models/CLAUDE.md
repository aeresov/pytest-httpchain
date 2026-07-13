# pytest_httpchain.models

Pydantic models for pytest-httpchain HTTP test scenarios.

## Purpose

This subpackage provides strongly-typed Pydantic models for defining HTTP test scenarios, including:
- Request/response structure validation
- Multiple body types (JSON, XML, Form, GraphQL, etc.)
- Variable substitutions and user functions
- Response verification and data extraction
- Test parameterization and parallel execution

## Subpackage Structure

```
src/pytest_httpchain/models/
├── __init__.py     # Public API exports
├── entities.py     # Pydantic models (Scenario, Stage, Request, etc.)
└── types.py        # Custom type validators and type aliases
```

## Public API

```python
from pytest_httpchain.models import (
    # Core models
    Scenario, Stage, Request,
    # Body types
    JsonBody, XmlBody, FormBody, TextBody, Base64Body, BinaryBody, FilesBody, GraphQLBody,
    # Substitutions
    Substitution, VarsSubstitution, FunctionsSubstitution,
    # Response handling
    Verify, Save, JMESPathSave, SubstitutionsSave, UserFunctionsSave,
    # Parameterization
    Parameter, IndividualParameter, CombinationsParameter,
    # Parallel execution
    ParallelConfig, ParallelConfigBase, ParallelRepeatConfig, ParallelForeachConfig,
    # Utilities
    check_json_schema,
)
```

## Running Tests

```bash
uv run pytest tests/unit/models -v
```

## Common Patterns

### Discriminated Unions
Body types, substitutions, and save types use Pydantic discriminators based on field presence for automatic type detection.

### Strict Validation
All models derive from `StrictModel` (`extra="forbid"` + a before-validator): unknown keys are rejected, so typos fail at validation instead of silently changing behavior. The one exception is `"$schema"` (editor metadata), which `StrictModel` drops from any dict a model consumes; `"$schema"` inside plain dict *values* (e.g. an inline response-body JSON Schema) is preserved.

### Input normalization
Substitutions, Responses, and Stages accept either a list OR a name-keyed mapping. `_normalize_list_input` flattens a dict's values into a list (list values are extended in, scalars are appended). `_normalize_stages_input` turns a dict into a list where each KEY overrides/becomes the stage's `name` field.

### Namespace conversion
`VarsSubstitution.vars` values are converted dict->`SimpleNamespace` (so `{{ var.attr }}` attribute access works in templates). `JsonBody.json` and GraphQL `variables` convert `SimpleNamespace`->dict for JSON serialization (`NamespaceOrDict`).

### Two-phase validation
Models validate twice. First with `{{ }}` template strings treated as opaque (`TemplateExpression` / `PartialTemplateStr` / `Any`), then again after the templates engine renders them at runtime, just before consumption — so the rendered concrete value is validated against the real type.

### Validated string types (types.py)
`Annotated` aliases that validate a field's contents:
- `JMESPathExpression` — JMESPath expression
- `RegexPattern` — regular expression
- `XMLString` — XML
- `GraphQLQuery` — GraphQL query
- `Base64String` — base64 encoding
- `TemplateExpression` — a complete `{{ ... }}` template (entire string is one expression)
- `PartialTemplateStr` — a string that may contain `{{ ... }}` templates inline
- `FunctionImportName` — a dotted function import name
- `VariableName` — a valid Python identifier
- `JSONSchemaInline` — an inline JSON Schema dict
- `SerializablePath` — a `Path` serialized to string
