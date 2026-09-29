"""Scenario validation, shared by the ``pytest-httpchain validate`` CLI and pytest collection.

Structural validation comes from the Pydantic ``Scenario`` model; this package
adds the semantic checks a JSON Schema cannot express. Every finding is a
`Diagnostic` with a stable ``HTTPCHAINxxx`` code (see docs/diagnostics.md).

- `loader`: load + ``$ref``-resolve + model-validate
- `semantic`: checks run everywhere, including at collection
- `deep`: opt-in checks that import user code and touch the filesystem
- `validate`: the entry points tying the three together, per file and per
  command-line path
- `discovery`: the scenario files a directory holds, found as pytest collection
  finds them
"""

from pytest_httpchain.validation.deep import check_scenario_deep
from pytest_httpchain.validation.diagnostics import SEVERITY, Diagnostic, DiagnosticCode, ScenarioInfo, Severity, ValidateResult
from pytest_httpchain.validation.discovery import DiscoveryError, configured_suffix, find_scenario_files
from pytest_httpchain.validation.loader import (
    is_alternatives_position,
    is_expected_value_position,
    is_inline_schema_position,
    load_scenario,
    load_scenario_json,
    load_with_diagnostics,
    merges_whole,
    resolve_root_path,
)
from pytest_httpchain.validation.semantic import check_scenario, describe_scenario
from pytest_httpchain.validation.validate import validate_paths, validate_scenario

__all__ = [
    "SEVERITY",
    "Diagnostic",
    "DiagnosticCode",
    "DiscoveryError",
    "ScenarioInfo",
    "Severity",
    "ValidateResult",
    "check_scenario",
    "check_scenario_deep",
    "configured_suffix",
    "describe_scenario",
    "find_scenario_files",
    "is_alternatives_position",
    "is_expected_value_position",
    "is_inline_schema_position",
    "load_scenario",
    "load_scenario_json",
    "load_with_diagnostics",
    "merges_whole",
    "resolve_root_path",
    "validate_paths",
    "validate_scenario",
]
