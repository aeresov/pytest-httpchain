"""Diagnostic codes and the result types the validator reports.

Codes are append-only and documented in docs/diagnostics.md.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

type Severity = Literal["error", "warning", "info"]


class DiagnosticCode(StrEnum):
    """A ``StrEnum`` so ``Diagnostic.code`` rejects unregistered codes at
    validation time; members interpolate as their values."""

    SCHEMA = "HTTPCHAIN000"
    DUPLICATE_STAGE = "HTTPCHAIN001"
    FIXTURE_CONFLICT = "HTTPCHAIN002"
    UNDEFINED_VAR = "HTTPCHAIN003"
    FORWARD_REF = "HTTPCHAIN004"
    NO_VERIFY = "HTTPCHAIN005"
    NOOP_VERIFY = "HTTPCHAIN006"
    CONTAINS_CONTRADICTION = "HTTPCHAIN007"
    MATCHES_CONTRADICTION = "HTTPCHAIN008"
    FIXTURE_SHADOWS_SAVE = "HTTPCHAIN009"
    FILE_NOT_FOUND = "HTTPCHAIN010"
    NOT_A_FILE = "HTTPCHAIN011"
    REF_ERROR = "HTTPCHAIN012"
    WRONG_EXTENSION = "HTTPCHAIN013"
    INVALID_JSON = "HTTPCHAIN014"
    PARSE_ERROR = "HTTPCHAIN015"
    FIXTURE_IN_SCENARIO_TEMPLATE = "HTTPCHAIN016"
    SCENARIO_UNDEFINED_VAR = "HTTPCHAIN017"
    NONTEMPLATE_EXPRESSION = "HTTPCHAIN018"
    INVALID_MARKER = "HTTPCHAIN019"
    REFERENCED_FILE_NOT_FOUND = "HTTPCHAIN020"
    SCHEMA_FILE_INVALID = "HTTPCHAIN021"
    IMPORT_FAILED = "HTTPCHAIN022"
    UNKNOWN_ARG = "HTTPCHAIN023"
    MISSING_ARG = "HTTPCHAIN024"
    PARAMETRIZE_COLLECTION_RESOLUTION = "HTTPCHAIN025"
    AMBIGUOUS_REF = "HTTPCHAIN026"
    RESERVED_NAME = "HTTPCHAIN027"
    SCHEMA_SCENARIO_DIRECTIVE = "HTTPCHAIN028"
    TEMPLATE_IN_KEY = "HTTPCHAIN029"


# A code's severity is a property of the code, not of the site that raises it:
# `plugin.JsonModule.collect` turns error-severity findings into a CollectError
# and warning-severity ones into a ScenarioValidationWarning, so typing the
# severity per call site let one stray "error" silently promote a documented
# warning into a collection failure. A test asserts this map covers every code
# and agrees with the table in docs/diagnostics.md.
SEVERITY: dict[DiagnosticCode, Severity] = {
    DiagnosticCode.SCHEMA: "error",
    DiagnosticCode.DUPLICATE_STAGE: "error",
    DiagnosticCode.FIXTURE_CONFLICT: "error",
    DiagnosticCode.UNDEFINED_VAR: "warning",
    DiagnosticCode.FORWARD_REF: "warning",
    DiagnosticCode.NO_VERIFY: "warning",
    DiagnosticCode.NOOP_VERIFY: "warning",
    DiagnosticCode.CONTAINS_CONTRADICTION: "error",
    DiagnosticCode.MATCHES_CONTRADICTION: "error",
    DiagnosticCode.FIXTURE_SHADOWS_SAVE: "warning",
    DiagnosticCode.FILE_NOT_FOUND: "error",
    DiagnosticCode.NOT_A_FILE: "error",
    DiagnosticCode.REF_ERROR: "error",
    DiagnosticCode.WRONG_EXTENSION: "warning",
    DiagnosticCode.INVALID_JSON: "error",
    DiagnosticCode.PARSE_ERROR: "error",
    DiagnosticCode.FIXTURE_IN_SCENARIO_TEMPLATE: "error",
    DiagnosticCode.SCENARIO_UNDEFINED_VAR: "error",
    DiagnosticCode.NONTEMPLATE_EXPRESSION: "warning",
    DiagnosticCode.INVALID_MARKER: "error",
    DiagnosticCode.REFERENCED_FILE_NOT_FOUND: "warning",
    DiagnosticCode.SCHEMA_FILE_INVALID: "warning",
    DiagnosticCode.IMPORT_FAILED: "warning",
    DiagnosticCode.UNKNOWN_ARG: "warning",
    DiagnosticCode.MISSING_ARG: "warning",
    DiagnosticCode.PARAMETRIZE_COLLECTION_RESOLUTION: "info",
    DiagnosticCode.AMBIGUOUS_REF: "warning",
    DiagnosticCode.RESERVED_NAME: "warning",
    DiagnosticCode.SCHEMA_SCENARIO_DIRECTIVE: "warning",
    DiagnosticCode.TEMPLATE_IN_KEY: "warning",
}


class Diagnostic(BaseModel):
    """A single validation finding."""

    code: DiagnosticCode
    severity: Severity
    message: str
    location: str | None = None


class ScenarioInfo(BaseModel):
    """Structural summary of a validated scenario."""

    num_stages: int = 0
    stage_names: list[str] = []
    vars_referenced: list[str] = []
    vars_saved: list[str] = []
    vars_defined: list[str] = []
    fixtures: list[str] = []


class ValidateResult(BaseModel):
    """Result of validating one scenario file."""

    valid: bool
    errors: list[str] = []
    warnings: list[str] = []
    diagnostics: list[Diagnostic] = []
    scenario_info: ScenarioInfo | None = None


def diag(code: DiagnosticCode, message: str, location: str | None = None) -> Diagnostic:
    return Diagnostic(code=code, severity=SEVERITY[code], message=message, location=location)


def result(diagnostics: list[Diagnostic], scenario_info: ScenarioInfo | None = None) -> ValidateResult:
    errors = [d.message for d in diagnostics if d.severity == "error"]
    return ValidateResult(
        valid=not errors,
        errors=errors,
        warnings=[d.message for d in diagnostics if d.severity == "warning"],
        diagnostics=diagnostics,
        scenario_info=scenario_info,
    )
