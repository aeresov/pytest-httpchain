# pytest-httpchain-models

The Pydantic models that define and validate
[pytest-httpchain](https://github.com/aeresov/pytest-httpchain) test scenarios.

This package is the typed schema for a scenario file. It models the whole
structure — `Scenario`, `Stage`, `Request`, the discriminated body types (JSON,
XML, form, text, base64, binary, files, GraphQL), variable substitutions,
response verification and save steps, parametrization, and parallel execution
configs. Every model derives from a strict base (`extra="forbid"`), so a
misspelled field is rejected at validation time instead of silently changing a
request. Validation runs in two phases: first with `{{ }}` template strings
treated as opaque, then again on the concrete values after the templates engine
renders them, so the rendered value is checked against its real type. The
package also generates the editor-facing JSON Schema published with the project.

## Role in the workspace

After [pytest-httpchain-jsonref](https://pypi.org/project/pytest-httpchain-jsonref/)
inlines a scenario's `$ref` directives, the plugin validates the resulting
document against `Scenario` here. These models are the single source of truth
for what a scenario may contain — both at pytest collection time and for the
JSON Schema editors consume.

## Usage

```python
from pytest_httpchain_models.entities import Scenario

scenario = Scenario.model_validate(
    {
        "stages": [
            {
                "name": "health",
                "request": {"url": "https://api.example.com/health"},
                "response": [{"verify": {"status": 200}}],
            }
        ]
    }
)
```

## Links

- Documentation: <https://aeresov.github.io/pytest-httpchain/>
- Source and issues: <https://github.com/aeresov/pytest-httpchain>
