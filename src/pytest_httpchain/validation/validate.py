"""File-level validation: load a scenario file and report everything found."""

import json
import warnings
from pathlib import Path

from pydantic import ValidationError

from pytest_httpchain.jsonref import DuplicateKeyError, ReferenceResolverError
from pytest_httpchain.validation.deep import check_scenario_deep
from pytest_httpchain.validation.diagnostics import Diagnostic, DiagnosticCode, ValidateResult, diag, result
from pytest_httpchain.validation.loader import load_scenario
from pytest_httpchain.validation.semantic import check_scenario
from pytest_httpchain.warnings import AmbiguousReferenceWarning


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
        return result([diag(DiagnosticCode.FILE_NOT_FOUND, "error", f"File not found: {path}")])

    if not path.is_file():
        return result([diag(DiagnosticCode.NOT_A_FILE, "error", f"Path is not a file: {path}")])

    if path.suffix.lower() != ".json":
        diagnostics.append(
            diag(
                DiagnosticCode.WRONG_EXTENSION,
                "warning",
                f"File has extension '{path.suffix}' but expected '.json'. Consider renaming to use .json extension.",
                location=str(path),
            )
        )

    # Load errors are collected rather than returned from inside the block, so
    # ambiguity warnings earned by earlier references are still reported.
    load_failed = False
    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        try:
            scenario, test_data = load_scenario(path, root_path=root_path, ref_parent_traversal_depth=ref_parent_traversal_depth)
        except ReferenceResolverError as e:
            # A duplicate key, and a plain syntax error the resolver wrapped, are
            # JSON content problems — no reference is involved in either.
            if isinstance(e, DuplicateKeyError):
                diagnostics.append(diag(DiagnosticCode.INVALID_JSON, "error", f"Invalid JSON: {e}"))
            elif isinstance(e.__cause__, json.JSONDecodeError):
                diagnostics.append(diag(DiagnosticCode.INVALID_JSON, "error", f"Invalid JSON syntax: {e.__cause__}"))
            else:
                diagnostics.append(diag(DiagnosticCode.REF_ERROR, "error", f"JSON reference resolution error: {e}"))
            load_failed = True
        except json.JSONDecodeError as e:
            diagnostics.append(diag(DiagnosticCode.INVALID_JSON, "error", f"Invalid JSON syntax: {e}"))
            load_failed = True
        except ValidationError as e:
            for err in e.errors():
                loc = " -> ".join(str(x) for x in err["loc"])
                diagnostics.append(diag(DiagnosticCode.SCHEMA, "error", f"Schema validation failed: {loc}: {err['msg']}", location=loc))
            load_failed = True
        except Exception as e:
            diagnostics.append(diag(DiagnosticCode.PARSE_ERROR, "error", f"Failed to parse JSON file: {e}"))
            load_failed = True

    for caught in caught_warnings:
        if isinstance(caught.message, AmbiguousReferenceWarning):
            diagnostics.append(diag(DiagnosticCode.AMBIGUOUS_REF, "warning", str(caught.message)))
        else:
            warnings.warn_explicit(caught.message, caught.category, caught.filename, caught.lineno)

    if load_failed:
        return result(diagnostics)

    semantic_diagnostics, scenario_info = check_scenario(scenario, test_data)
    diagnostics.extend(semantic_diagnostics)

    if deep:
        diagnostics.extend(check_scenario_deep(scenario, syspaths=syspaths, scenario_dir=path.parent))

    return result(diagnostics, scenario_info)
