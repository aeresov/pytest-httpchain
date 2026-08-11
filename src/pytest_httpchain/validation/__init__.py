"""Scenario validation, shared by the ``pytest-httpchain validate`` CLI and pytest collection.

Structural validation comes from the Pydantic ``Scenario`` model; this package
adds the semantic checks a JSON Schema cannot express. Every finding is a
`Diagnostic` with a stable ``HTTPCHAINxxx`` code (see docs/diagnostics.md).

- `loader`: load + ``$ref``-resolve + model-validate
- `semantic`: checks run everywhere, including at collection
- `deep`: opt-in checks that import user code and touch the filesystem
- `validate`: the file-level entry point tying the three together
"""

from pytest_httpchain.validation.deep import check_scenario_deep
from pytest_httpchain.validation.diagnostics import Diagnostic, DiagnosticCode, ScenarioInfo, Severity, ValidateResult
from pytest_httpchain.validation.loader import is_inline_schema_position, load_scenario, resolve_root_path
from pytest_httpchain.validation.semantic import check_scenario
from pytest_httpchain.validation.validate import validate_scenario

__all__ = [
    "Diagnostic",
    "DiagnosticCode",
    "ScenarioInfo",
    "Severity",
    "ValidateResult",
    "check_scenario",
    "check_scenario_deep",
    "is_inline_schema_position",
    "load_scenario",
    "resolve_root_path",
    "validate_scenario",
]
