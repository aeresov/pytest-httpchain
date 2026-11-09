"""Opt-in checks that touch the filesystem and import user code (``validate --deep``).

Never run at collection time; every finding is a warning.
"""

import inspect
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pytest_httpchain.body_schema import UNBOUNDED, ReferenceBounds, SchemaFile, file_body_schema, inline_body_schema
from pytest_httpchain.connections import ADAPTER_METHODS, ProtocolAdapter
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
from pytest_httpchain.userfunc import UserFunctionError, call_target, import_adapter, import_function
from pytest_httpchain.utils import path_segment, resolve_scenario_path, schema_error_text
from pytest_httpchain.validation.diagnostics import Diagnostic, DiagnosticCode, diag


def check_scenario_deep(
    scenario: Scenario,
    syspaths: list[Path] | None = None,
    scenario_dir: Path | None = None,
    ref_bounds: ReferenceBounds = UNBOUNDED,
) -> list[Diagnostic]:
    """Referenced-file existence, user-function imports, call-signature
    compatibility, and connection adapters.

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
        diagnostics += list(_adapter_diagnostics(scenario))
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
        match stage.request.body if stage.request is not None else None:
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
    yield from _parameter_problems(signature, provided)


def _parameter_problems(signature: inspect.Signature, provided: set[str]) -> Iterator[tuple[DiagnosticCode, str]]:
    """`_signature_problems` for a signature in hand."""
    params = list(signature.parameters.values())
    keyword_acceptable = {p.name for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    required_keywords = {p.name for p in params if p.default is p.empty and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    required_positionals = {p.name for p in params if p.default is p.empty and p.kind is p.POSITIONAL_ONLY}

    if not any(p.kind is p.VAR_KEYWORD for p in params):
        for name in sorted(provided - keyword_acceptable):
            yield DiagnosticCode.UNKNOWN_ARG, f"unexpected argument '{name}'"
    # A keyword with the same name cannot fill a positional-only parameter.
    # With **kwargs it is accepted into kwargs, but the positional argument is
    # still missing when the function is called.
    for name in sorted(required_positionals | (required_keywords - provided)):
        yield DiagnosticCode.MISSING_ARG, f"missing required argument '{name}'"


def _function_diagnostics(scenario: Scenario) -> Iterator[Diagnostic]:
    """HTTPCHAIN022/023/024: resolve every literal user-function reference and,
    where the call arguments are statically known, check them against the
    signature: a verify or save function gets ``response``, or ``message`` on
    a connection stage."""
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
        if stage.request is not None:
            add_auth_site(stage.request.auth, f"stages[{i}].request.auth")
        add_substitution_sites(stage.substitutions, f"stages[{i}].substitutions")
        # What verify and save functions get their subject by: a connection
        # stage's message, or the HTTP response (`response_steps`).
        subject = {"message"} if stage.connection is not None else {"response"}
        for k, step in enumerate(stage.response):
            match step:
                case SaveStep(save=UserFunctionsSave(user_functions=calls)):
                    for j, call in enumerate(calls):
                        sites.append((call, subject, True, f"stages[{i}].response[{k}].save.user_functions[{j}]"))
                case SaveStep(save=SubstitutionsSave(substitutions=substitutions)):
                    add_substitution_sites(substitutions, f"stages[{i}].response[{k}].save.substitutions")
                case VerifyStep(verify=verify):
                    for j, call in enumerate(verify.user_functions):
                        sites.append((call, subject, True, f"stages[{i}].response[{k}].verify.user_functions[{j}]"))

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


# A placeholder for the instance a plain method is called on.
_SELF = object()


def _stub_owner(owner: type) -> bool:
    """Whether an attribute found on ``owner`` is no adapter's own: the
    plugin's `ProtocolAdapter`, whose methods are stubs that do nothing, or
    ``typing``'s and ``object``'s machinery."""
    return owner is ProtocolAdapter or owner is object or owner.__module__ == "typing"


def _constructor_signature(adapter: type) -> inspect.Signature:
    """What ``adapter(**params)`` accepts. A class subclassing a
    ``typing.Protocol`` without an ``__init__`` of its own gets typing's
    placeholder, whose ``(*args, **kwargs)`` hides that it takes no
    arguments at all, as ``object`` does: that is the signature returned
    then. Raises as `inspect.signature` does."""
    owner = next((klass for klass in adapter.__mro__ if "__init__" in vars(klass)), object)
    if getattr(vars(owner)["__init__"], "__module__", None) == "typing":
        return inspect.Signature()
    return inspect.signature(adapter)


def _method_problem(adapter: type, method: str, args: tuple[str, ...]) -> str | None:
    """Why ``adapter`` cannot be called as the plugin calls ``method``, with
    ``args`` positionally, or None. Missing, or only a `ProtocolAdapter` stub
    (found through the MRO), is a problem; so is a signature that cannot bind
    the arguments. The attribute is classified as it is declared
    (`inspect.getattr_static`): a plain function binds the instance first, a
    staticmethod does not, and a classmethod comes bound. A signature that
    cannot be read (a C method) is not judged."""
    owner = next((klass for klass in adapter.__mro__ if method in vars(klass)), None)
    if owner is None or _stub_owner(owner):
        inherited = " (only ProtocolAdapter's stub, which does nothing)" if owner is ProtocolAdapter else ""
        return f"has no {method}() method{inherited}"
    declared = inspect.getattr_static(adapter, method)
    match declared:
        case staticmethod():
            target, bound = declared.__func__, args
        case classmethod():
            target, bound = getattr(adapter, method), args
        case _ if inspect.isfunction(declared):
            target, bound = declared, (_SELF, *args)
        case _ if callable(declared):
            target, bound = declared, args
        case _:
            return f"has a {method} that is no method ({type(declared).__name__})"
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return None
    try:
        signature.bind(*bound)
    except TypeError as e:
        return f"{method}{signature} cannot be called as {method}({', '.join(args)}): {e}"
    return None


def _adapter_diagnostics(scenario: Scenario) -> Iterator[Diagnostic]:
    """HTTPCHAIN046: each connection's adapter, as the plugin uses it when a
    stage opens the connection: imported (`userfunc.import_adapter`, which
    refuses what is no class), each method of `connections.ADAPTER_METHODS`
    its own and callable with the arguments the plugin passes, positionally,
    and its constructor taking the connection's ``params`` as keyword
    arguments (the keys are known even where the values are templates).

    Its own code, not 022/023/024: those are documented as a user
    function's, and an adapter is a class."""
    for name, config in scenario.connections.items():
        location = f"connections.{name}.adapter"
        try:
            adapter = import_adapter(config.adapter)
        except UserFunctionError as e:
            yield diag(DiagnosticCode.ADAPTER_INVALID, f"Connection '{name}': cannot import adapter '{config.adapter}': {e}", location)
            continue
        for method, args in ADAPTER_METHODS.items():
            if (problem := _method_problem(adapter, method, args)) is not None:
                yield diag(
                    DiagnosticCode.ADAPTER_INVALID,
                    f"Connection '{name}': adapter '{config.adapter}' {problem}; the plugin calls {method}({', '.join(args)}) on it. Define it to take those arguments.",
                    location,
                )
        try:
            signature = _constructor_signature(adapter)
        except (TypeError, ValueError):
            continue
        for _, problem in _parameter_problems(signature, set(config.params)):
            yield diag(
                DiagnosticCode.ADAPTER_INVALID,
                f"Connection '{name}': adapter '{config.adapter}' cannot be built with the connection's params: {problem}",
                f"connections.{name}.params",
            )
