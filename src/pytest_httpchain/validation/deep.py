"""Opt-in checks that touch the filesystem and import user code (``validate --deep``).

Never run at collection time; every finding is a warning.
"""

import inspect
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pytest_httpchain.body_schema import UNBOUNDED, ReferenceBounds, SchemaFile, file_body_schema, inline_body_schema
from pytest_httpchain.errors import SchemaFileError, SchemaPointerError
from pytest_httpchain.models import (
    BinaryBody,
    FilesBody,
    FileSpec,
    FunctionsSubstitution,
    Multipart,
    MultipartBody,
    SaveStep,
    Scenario,
    SubstitutionsSave,
    UserFunctionCall,
    UserFunctionKwargs,
    UserFunctionName,
    UserFunctionsSave,
    VerifyStep,
)
from pytest_httpchain.templates import contains_template, unescape
from pytest_httpchain.userfunc import UserFunctionError, call_target, import_function
from pytest_httpchain.utils import path_segment, resolve_scenario_path, schema_error_text
from pytest_httpchain.validation.diagnostics import Diagnostic, DiagnosticCode, diag


def check_scenario_deep(
    scenario: Scenario,
    syspaths: list[Path] | None = None,
    scenario_dir: Path | None = None,
    ref_bounds: ReferenceBounds = UNBOUNDED,
) -> list[Diagnostic]:
    """Referenced-file existence, user-function imports, and call-signature compatibility.

    Imports user modules, so ``syspaths`` (and the CWD) are temporarily prepended
    to ``sys.path`` to resolve them the way pytest would. ``ref_bounds`` is
    what a body schema's references to files are held to, the root and the
    parent traversal depth the scenario's own references were loaded under.
    """
    diagnostics = list(_file_diagnostics(scenario, scenario_dir, ref_bounds))

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
    """The file a path value names before any stage runs, else None (missing
    values, inline schemas, and a path holding a template, known only once
    rendered).

    A path field holds a path that rendering changes as text, not as a
    `Path` (`types.SerializablePath`): one with only an escaped ``\\{{``
    names the file it renders to, the braces the file's own (`unescape`), as
    the runtime opens it."""
    match value:
        case Path():
            return value
        case str() if not contains_template(value):
            return Path(unescape(value))
        case _:
            return None


def _check_path_value(value: Any, location: str, base_dir: Path | None = None) -> Iterator[Diagnostic]:
    """HTTPCHAIN020 for a literal path (or tuple/list of them)."""
    if isinstance(value, tuple | list):
        for idx, item in enumerate(value):
            yield from _check_path_value(item, f"{location}[{idx}]", base_dir)
        return
    path = _literal_path(value)
    if path is not None and not resolve_scenario_path(base_dir, path).exists():
        yield diag(DiagnosticCode.REFERENCED_FILE_NOT_FOUND, f"Referenced file not found: {path}", location)


def _check_file(entry: Any, location: str, base_dir: Path | None) -> Iterator[Diagnostic]:
    """HTTPCHAIN020 for the literal path a multipart body's file names: a path,
    or a file object's ``path``, and a list's items one at a time. Content given
    inline (``content``, ``base64``) reads no file."""
    if isinstance(entry, list):
        for index, item in enumerate(entry):
            yield from _check_file(item, f"{location}[{index}]", base_dir)
    elif isinstance(entry, FileSpec):
        yield from _check_path_value(entry.path, f"{location}.path", base_dir)
    else:
        yield from _check_path_value(entry, location, base_dir)


def _check_schema(schema: Any, location: str, base_dir: Path | None, ref_bounds: ReferenceBounds) -> Iterator[Diagnostic]:
    """HTTPCHAIN020/021 for a body schema: a literal file reference's file
    exists and is JSON, its pointer leads somewhere, the schema it selects is
    valid, and, in any schema, every ``$ref`` it reaches resolves to a valid
    schema, as the runtime resolves them (`body_schema`). A file that is not
    there is HTTPCHAIN020, anything else HTTPCHAIN021."""
    match schema:
        case dict():
            # Meta-checked by the model already.
            body = inline_body_schema(schema, base_dir, ref_bounds)
        case str() if not contains_template(schema):
            file = SchemaFile.locate(schema, base_dir)
            if not file.path.exists():
                # Named with the pointer, and escaped (a NUL, one JSON \u escape away).
                yield diag(DiagnosticCode.REFERENCED_FILE_NOT_FOUND, f"Schema file not found: {file}", location)
                return
            try:
                body = file_body_schema(file, ref_bounds)
            except SchemaFileError as e:
                yield diag(DiagnosticCode.SCHEMA_FILE_INVALID, f"Schema file is not valid JSON: {file.path}: {e}", location)
                return
            except SchemaPointerError as e:
                yield diag(DiagnosticCode.SCHEMA_FILE_INVALID, f"Schema pointer '#{file.fragment}' leads nowhere in {file.path}: {e}", location)
                return
            try:
                body.check()
            except Exception as e:
                yield diag(DiagnosticCode.SCHEMA_FILE_INVALID, f"Schema file is not a valid JSON Schema: {file}: {schema_error_text(e)}", location)
                return
        case _:
            return
    subject = body.where[0].upper() + body.where[1:]
    try:
        for reason, missing in body.unresolvable():
            code = DiagnosticCode.REFERENCED_FILE_NOT_FOUND if missing else DiagnosticCode.SCHEMA_FILE_INVALID
            yield diag(code, f"{subject}: {reason}", location)
    except Exception as e:
        # A document the walk cannot read, reported as one, not a traceback
        # that ends the whole `validate --deep` run, every other file's
        # findings with it.
        yield diag(DiagnosticCode.SCHEMA_FILE_INVALID, f"{subject}: its references cannot be checked: {e}", location)


def _file_diagnostics(scenario: Scenario, base_dir: Path | None = None, ref_bounds: ReferenceBounds = UNBOUNDED) -> Iterator[Diagnostic]:
    """Every literal filesystem path the scenario references, resolved against
    the scenario file's directory as the runtime does, and every file its body
    schemas reference, held to ``ref_bounds`` as the runtime holds them."""
    yield from _check_path_value(scenario.ssl.cert, "ssl.cert", base_dir)
    yield from _check_path_value(scenario.ssl.verify, "ssl.verify", base_dir)

    for i, stage in enumerate(scenario.stages):
        match stage.request.body:
            case BinaryBody(binary=binary):
                yield from _check_path_value(binary, f"stages[{i}].request.body.binary", base_dir)
            case FilesBody(files=files):
                for field, entry in files.items():
                    yield from _check_file(entry, f"stages[{i}].request.body.files{path_segment(field)}", base_dir)
            case MultipartBody(multipart=Multipart(files=files)):
                for field, entry in files.items():
                    yield from _check_file(entry, f"stages[{i}].request.body.multipart.files{path_segment(field)}", base_dir)
            case _:
                pass

        for k, step in enumerate(stage.response):
            if isinstance(step, VerifyStep):
                yield from _check_schema(step.verify.body.schema, f"stages[{i}].response[{k}].verify.body.schema", base_dir, ref_bounds)


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

    def add_auth_site(auth: Any, location: str) -> None:
        # Only a user function: the built-in schemes and `false` import nothing.
        if isinstance(auth, UserFunctionName | UserFunctionKwargs):
            sites.append((auth, set(), True, location))

    add_auth_site(scenario.auth, "auth")
    add_substitution_sites(scenario.substitutions, "substitutions")

    for i, stage in enumerate(scenario.stages):
        add_auth_site(stage.request.auth, f"stages[{i}].request.auth")
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
        if contains_template(name):
            continue  # template form — the real name is only known at runtime
        try:
            func = import_function(name)
        except UserFunctionError as e:
            yield diag(DiagnosticCode.IMPORT_FAILED, f"Cannot import function '{name}': {e}", location)
            continue
        if not check_signature:
            continue
        for code, problem in _signature_problems(func, injected | kwargs.keys()):
            yield diag(code, f"Function '{name}': {problem}", location)
