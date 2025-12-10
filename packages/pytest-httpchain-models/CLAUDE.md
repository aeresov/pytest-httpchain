# pytest-httpchain-models

Pydantic models for pytest-httpchain HTTP test scenarios.

## Purpose

This package provides strongly-typed Pydantic models for defining HTTP test scenarios, including:
- Request/response structure validation
- Multiple body types (JSON, XML, Form, GraphQL, etc.)
- Variable substitutions and user functions
- Response verification and data extraction
- Test parameterization and parallel execution

## Package Structure

```
src/pytest_httpchain_models/
├── __init__.py     # Public API exports
├── entities.py     # Pydantic models (Scenario, Stage, Request, etc.)
└── types.py        # Custom type validators and type aliases
```

## Public API

```python
from pytest_httpchain_models import (
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
    ParallelConfig, ParallelRepeatConfig, ParallelForeachConfig,
    # Utilities
    check_json_schema,
)
```

## Running Tests

```bash
# From monorepo root
uv run pytest packages/pytest-httpchain-models/tests -v

# Or from package directory
cd packages/pytest-httpchain-models
uv run pytest tests -v
```

## Common Patterns

### Discriminated Unions
Body types, substitutions, and save types use Pydantic discriminators based on field presence for automatic type detection.
