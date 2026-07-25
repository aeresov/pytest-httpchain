"""Diagnostic codes and the result types the validator reports.

Codes are append-only and documented in docs/diagnostics.md.
"""

from typing import Literal

from pydantic import BaseModel

Severity = Literal["error", "warning", "info"]


class DiagnosticCode:
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


class Diagnostic(BaseModel):
    """A single validation finding."""

    code: str
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


def diag(code: str, severity: Severity, message: str, location: str | None = None) -> Diagnostic:
    return Diagnostic(code=code, severity=severity, message=message, location=location)


def result(diagnostics: list[Diagnostic], scenario_info: ScenarioInfo | None = None) -> ValidateResult:
    errors = [d.message for d in diagnostics if d.severity == "error"]
    return ValidateResult(
        valid=not errors,
        errors=errors,
        warnings=[d.message for d in diagnostics if d.severity == "warning"],
        diagnostics=diagnostics,
        scenario_info=scenario_info,
    )
