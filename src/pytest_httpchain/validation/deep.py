"""Opt-in checks that touch the filesystem and import user code (``validate --deep``).

Never run at collection time; every finding is a warning.
"""

import inspect
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pytest_httpchain.models import (
    BinaryBody,
    FilesBody,
    FunctionsSubstitution,
    SaveStep,
    Scenario,
    SubstitutionsSave,
    UserFunctionCall,
    UserFunctionsSave,
    VerifyStep,
    check_json_schema,
)
from pytest_httpchain.userfunc import UserFunctionError, call_target, import_function
from pytest_httpchain.utils import resolve_scenario_path
from pytest_httpchain.validation.diagnostics import Diagnostic, DiagnosticCode, diag


def check_scenario_deep(scenario: Scenario, syspaths: list[Path] | None = None, scenario_dir: Path | None = None) -> list[Diagnostic]:
    """Referenced-file existence, user-function imports, and call-signature compatibility.

    Imports user modules, so ``syspaths`` (and the CWD) are temporarily prepended
    to ``sys.path`` to resolve them the way pytest would.
    """
    diagnostics = list(_file_diagnostics(scenario, scenario_dir))

    saved_path = list(sys.path)
    try:
        for entry in [*(str(Path(p).resolve()) for p in (syspaths or [])), str(Path.cwd())]:
            if entry not in sys.path:
                sys.path.insert(0, entry)
        # Consumed inside the try: the checks import user modules, so they must
        # run while the temporary sys.path entries are in place.
        diagnostics += list(_function_diagnostics(scenario))
    finally:
        sys.path[:] = saved_path

    return diagnostics


def _literal_path(value: Any) -> Path | None:
    """A concrete filesystem path for a literal path value, else None (missing
    values, inline schemas, and anything holding a ``{{ }}`` template)."""
    if value is None or isinstance(value, dict):
        return None
    if isinstance(value, Path):
        return None if "{{" in str(value) else value
    if isinstance(value, str):
        return None if "{{" in value else Path(value)
    return None


def _check_path_value(value: Any, location: str, base_dir: Path | None = None) -> Iterator[Diagnostic]:
    """HTTPCHAIN020 for a literal path (or tuple/list of them)."""
    if isinstance(value, tuple | list):
        for idx, item in enumerate(value):
            yield from _check_path_value(item, f"{location}[{idx}]", base_dir)
        return
    path = _literal_path(value)
    if path is not None and not resolve_scenario_path(base_dir, path).exists():
        yield diag(DiagnosticCode.REFERENCED_FILE_NOT_FOUND, "warning", f"Referenced file not found: {path}", location)


def _check_schema_path(schema: Any, location: str, base_dir: Path | None = None) -> Iterator[Diagnostic]:
    """HTTPCHAIN020/021 for a literal JSON-schema file path."""
    path = _literal_path(schema)
    if path is None:
        return
    path = resolve_scenario_path(base_dir, path)
    if not path.exists():
        yield diag(DiagnosticCode.REFERENCED_FILE_NOT_FOUND, "warning", f"Schema file not found: {path}", location)
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    # ValueError subsumes json.JSONDecodeError and UnicodeDecodeError, matching
    # the runtime read in response_steps._verify_body_schema.
    except (OSError, ValueError) as e:
        yield diag(DiagnosticCode.SCHEMA_FILE_INVALID, "warning", f"Schema file is not valid JSON: {path}: {e}", location)
        return
    try:
        check_json_schema(data)
    except Exception as e:
        yield diag(DiagnosticCode.SCHEMA_FILE_INVALID, "warning", f"Schema file is not a valid JSON Schema: {path}: {e}", location)


def _file_diagnostics(scenario: Scenario, base_dir: Path | None = None) -> Iterator[Diagnostic]:
    """Every literal filesystem path the scenario references, resolved against
    the scenario file's directory as the runtime does."""
    yield from _check_path_value(scenario.ssl.cert, "ssl.cert", base_dir)
    yield from _check_path_value(scenario.ssl.verify, "ssl.verify", base_dir)

    for i, stage in enumerate(scenario.stages):
        match stage.request.body:
            case BinaryBody(binary=binary):
                yield from _check_path_value(binary, f"stages[{i}].request.body.binary", base_dir)
            case FilesBody(files=files):
                for field, file_path in files.items():
                    yield from _check_path_value(file_path, f"stages[{i}].request.body.files.{field}", base_dir)
            case _:
                pass

        for k, step in enumerate(stage.response):
            if isinstance(step, VerifyStep):
                yield from _check_schema_path(step.verify.body.schema, f"stages[{i}].response[{k}].verify.body.schema", base_dir)


def _signature_problems(func: Any, provided: set[str]) -> Iterator[tuple[DiagnosticCode, str]]:
    """``(code, message)`` for each mismatch between the names a call supplies
    and the function's signature.

    Call sites pass everything by keyword, so a required positional-only
    parameter counts as unfillable rather than as satisfiable.
    """
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return  # not introspectable (some builtins/C functions)

    params = list(signature.parameters.values())
    keyword_acceptable = {p.name for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    required = {p.name for p in params if p.default is p.empty and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY, p.POSITIONAL_ONLY)}

    if not any(p.kind is p.VAR_KEYWORD for p in params):
        for name in sorted(provided - keyword_acceptable):
            yield DiagnosticCode.UNKNOWN_ARG, f"unexpected argument '{name}'"
    for name in sorted(required - provided):
        yield DiagnosticCode.MISSING_ARG, f"missing required argument '{name}'"


def _function_diagnostics(scenario: Scenario) -> Iterator[Diagnostic]:
    """HTTPCHAIN022/023/024: resolve every literal user-function reference and,
    where the call arguments are statically known, check them against the signature."""
    # (call, injected-arg-names, check_signature, location). Substitution
    # `functions` are invoked from templates with unknown call-time arguments,
    # so they are import-checked only.
    sites: list[tuple[UserFunctionCall, set[str], bool, str]] = []

    def add_substitution_sites(substitutions: list[Any], location_prefix: str) -> None:
        for sub in substitutions:
            match sub:
                case FunctionsSubstitution(functions=functions):
                    for alias, call in functions.items():
                        sites.append((call, set(), False, f"{location_prefix}.functions.{alias}"))

    if scenario.auth is not None:
        sites.append((scenario.auth, set(), True, "auth"))
    add_substitution_sites(scenario.substitutions, "substitutions")

    for i, stage in enumerate(scenario.stages):
        if stage.request.auth is not None:
            sites.append((stage.request.auth, set(), True, f"stages[{i}].request.auth"))
        add_substitution_sites(stage.substitutions, f"stages[{i}].substitutions")
        for k, step in enumerate(stage.response):
            match step:
                case SaveStep(save=UserFunctionsSave(user_functions=calls)):
                    for j, call in enumerate(calls):
                        sites.append((call, {"response"}, True, f"stages[{i}].response[{k}].save.user_functions[{j}]"))
                case SaveStep(save=SubstitutionsSave(substitutions=substitutions)):
                    add_substitution_sites(substitutions, f"stages[{i}].response[{k}].save.substitutions")
                case VerifyStep(verify=verify):
                    for j, call in enumerate(verify.user_functions):
                        sites.append((call, {"response"}, True, f"stages[{i}].response[{k}].verify.user_functions[{j}]"))

    for call, injected, check_signature, location in sites:
        name, kwargs = call_target(call)
        if "{{" in name:
            continue  # template form — the real name is only known at runtime
        try:
            func = import_function(name)
        except UserFunctionError as e:
            yield diag(DiagnosticCode.IMPORT_FAILED, "warning", f"Cannot import function '{name}': {e}", location)
            continue
        if not check_signature:
            continue
        for code, problem in _signature_problems(func, injected | kwargs.keys()):
            yield diag(code, "warning", f"Function '{name}': {problem}", location)
