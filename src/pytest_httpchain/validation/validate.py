"""File-level validation: load a scenario file and report everything found."""

from pathlib import Path

from pytest_httpchain.validation.deep import check_scenario_deep
from pytest_httpchain.validation.diagnostics import Diagnostic, DiagnosticCode, ValidateResult, diag, result
from pytest_httpchain.validation.loader import load_with_diagnostics
from pytest_httpchain.validation.semantic import check_scenario, describe_scenario


def validate_scenario(
    path: Path,
    ref_parent_traversal_depth: int = 3,
    root_path: Path | None = None,
    deep: bool = False,
    syspaths: list[Path] | None = None,
) -> ValidateResult:
    """Validate a scenario file: existence, JSON, ``$ref``, schema, then the
    semantic checks — and with ``deep``, the opt-in import/file checks."""
    diagnostics: list[Diagnostic] = []

    if not path.exists():
        return result([diag(DiagnosticCode.FILE_NOT_FOUND, f"File not found: {path}")])

    if not path.is_file():
        return result([diag(DiagnosticCode.NOT_A_FILE, f"Path is not a file: {path}")])

    if path.suffix.lower() != ".json":
        diagnostics.append(
            diag(
                DiagnosticCode.WRONG_EXTENSION,
                f"File has extension '{path.suffix}' but expected '.json'. Consider renaming to use .json extension.",
                location=str(path),
            )
        )

    # Load diagnostics are collected rather than returned early, so ambiguity
    # warnings earned by earlier references are still reported.
    loaded, load_diagnostics = load_with_diagnostics(path, root_path=root_path, ref_parent_traversal_depth=ref_parent_traversal_depth)
    diagnostics.extend(load_diagnostics)
    if loaded is None:
        return result(diagnostics)

    scenario, test_data = loaded
    diagnostics.extend(check_scenario(scenario, test_data))

    if deep:
        diagnostics.extend(check_scenario_deep(scenario, syspaths=syspaths, scenario_dir=path.parent))

    return result(diagnostics, describe_scenario(scenario, test_data))
