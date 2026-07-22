"""Static validation for pytest-httpchain scenario files.

This is the single source of truth for scenario validation, consumed by the
``pytest-httpchain validate`` CLI command, by collection-time validation in the
pytest plugin, and available for editor/CI integrations. It performs structural
validation via the Pydantic ``Scenario`` model plus cross-cutting *semantic*
checks that a JSON Schema cannot express.

Every finding is reported as a `Diagnostic` carrying a stable code
(``HTTPCHAINxxx``), a severity, a human-readable message and (where meaningful)
a location, so tooling can filter/sort/route diagnostics deterministically.

Diagnostic codes
----------------
======== ======== ==========================================================
Code     Severity Meaning
======== ======== ==========================================================
000      error    Schema validation failed (Pydantic ``Scenario`` model)
001      error    Duplicate stage names
002      error    Fixture and variable share the same name
003      warning  Variable referenced but never defined/saved/fixture (typo)
004      warning  Variable referenced before it is saved/defined (ordering / data-flow)
005      warning  Stage has no verify step (no response validation)
006      warning  Verify step asserts nothing (no-op)
007      error    Body ``contains``/``not_contains`` list the same substring
008      error    Body ``matches``/``not_matches`` list the same pattern
009      warning  Saved variable is shadowed by a scenario-level fixture
010      error    File not found
011      error    Path is not a file
012      error    ``$ref`` resolution failed
013      warning  File extension is not ``.json``
014      error    Invalid JSON syntax
015      error    Failed to parse JSON file
016      error    Fixture referenced in a scenario-level template
017      error    Scenario-level template references an undefined name
018      warning  Verify expression is not a template (``{{ }}``) — asserts nothing
019      error    Invalid pytest marker expression (scenario or stage ``marks``)
020      warning  Referenced file does not exist (deep, opt-in)
021      warning  Schema file is not valid JSON / not a valid schema (deep)
022      warning  User function cannot be imported (deep)
023      warning  Unexpected argument passed to a user function (deep)
024      warning  Missing required argument for a user function (deep)
025      info     Template parametrize values force collection-time resolution
026      warning  ``$ref`` path matches files under both lookup bases (ambiguous)
027      warning  User-defined name shadowed by the reserved ``response`` namespace
028      warning  Scenario directive (``$include``/``$merge``, or file-path ``$ref``) inside an inline JSON Schema — not resolved there
======== ======== ==========================================================

The ``020``–``024`` codes come from *deep* validation, which is opt-in
(``validate --deep``) because it imports user modules and touches the
filesystem; it never runs at collection time. Deep findings are always
warnings.

``info`` diagnostics are purely informational: they never affect validity,
are exempt from ``--strict``, and are not warned about at collection.
"""

import inspect
import json
import sys
import warnings
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from pytest_httpchain.jsonref import DuplicateKeyError, ReferenceResolverError, load_json
from pytest_httpchain.models import (
    BinaryBody,
    FilesBody,
    FunctionsSubstitution,
    HeaderMatcher,
    SaveStep,
    Scenario,
    UserFunctionCall,
    UserFunctionKwargs,
    UserFunctionName,
    UserFunctionsSave,
    Verify,
    VerifyStep,
    check_json_schema,
    parametrize_values_contain_template,
)
from pytest_httpchain.scoping import (
    RESPONSE_META_NAME,
    extract_defined_variables,
    extract_saved_variables,
    extract_template_variables,
    raw_stages,
    raw_substitution_entries,
    raw_substitution_entry_names,
    raw_substitution_entry_templates,
    stage_scopes,
    substitution_names,
)
from pytest_httpchain.templates import is_complete_template
from pytest_httpchain.userfunc import UserFunctionError, import_function
from pytest_httpchain.utils import make_marker
from pytest_httpchain.warnings import AmbiguousReferenceWarning

Severity = Literal["error", "warning", "info"]


class DiagnosticCode:
    """Stable diagnostic codes (see module docstring for the full table)."""

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
    # Deep (opt-in) checks: imports, signatures, referenced files.
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


def _diag(code: str, severity: Severity, message: str, location: str | None = None) -> Diagnostic:
    return Diagnostic(code=code, severity=severity, message=message, location=location)


def _is_noop_verify(verify: Verify) -> bool:
    """A verify step that asserts nothing: no status, headers, expressions,
    user functions, schema, or body substring/regex checks."""
    body = verify.body
    return (
        verify.status is None
        and not verify.headers
        and not verify.expressions
        and not verify.user_functions
        and body.schema is None
        and not body.contains
        and not body.not_contains
        and not body.matches
        and not body.not_matches
    )


def _dataflow_diagnostics(scenario: Scenario, test_data: dict[str, Any]) -> list[Diagnostic]:
    """Order-aware variable data-flow analysis.

    Walks stages in execution order checking each template reference against
    the phase scopes computed by ``scoping.stage_scopes`` — the single encoding
    of the runtime visibility rules (see that module's docstring for the full
    table). Intra-step ordering within a stage's response is approximated: all
    of a stage's own saves are treated as available to its whole response.

    A reference that is unavailable is reported as `DiagnosticCode.FORWARD_REF`
    if the name is saved somewhere later, or defined by a later substitution
    step of the same stage (both ordering bugs) — or
    `DiagnosticCode.UNDEFINED_VAR` otherwise (likely a typo).
    """
    diagnostics: list[Diagnostic] = []

    scopes = stage_scopes(scenario)
    all_saved = extract_saved_variables(scenario)
    first_save_stage: dict[str, int] = {}
    for i, scope in enumerate(scopes):
        for name in scope.saves:
            first_save_stage.setdefault(name, i)

    # raws[i] is the unvalidated dict for scenario.stages[i]: both come from the same
    # order-preserving normalization (_normalize_stages_input), so they pair by index.
    raws = raw_stages(test_data)

    for i, stage in enumerate(scenario.stages):
        scope = scopes[i]
        raw = raws[i] if i < len(raws) and isinstance(raws[i], dict) else {}

        pre_iteration_refs = extract_template_variables(raw.get("parallel"))
        request_refs = extract_template_variables(raw.get("request"))
        response_refs = extract_template_variables(raw.get("response"))

        # ``parametrize`` parameter VALUES are resolved at collection time
        # (carrier.create_test_class) against scenario-level substitutions only — no
        # fixtures, no stage substitutions, no parameter names, no saved values
        # exist yet.
        for name in sorted(extract_template_variables(raw.get("parametrize"))):
            if name in scope.scenario_substitutions:
                continue
            diagnostics.append(
                _diag(
                    DiagnosticCode.UNDEFINED_VAR,
                    "warning",
                    f"Stage '{stage.name}': parametrize value references '{name}' — only scenario-level substitutions are in scope when values are resolved",
                    location=stage.name,
                )
            )

        # ``always_run`` resolves at stage start (carrier.execute_stage) against
        # fixtures + parametrize parameters + global context — stage substitutions
        # and foreach parameters don't exist yet, and neither do this stage's saves.
        for name in sorted(extract_template_variables(raw.get("always_run"))):
            if name in scope.always_run:
                continue
            if name in all_saved:
                j = first_save_stage[name]
                if j == i:
                    msg = f"Stage '{stage.name}': always_run references '{name}', which is only saved in this stage's response — always_run is evaluated before the stage runs"
                else:
                    msg = f"Stage '{stage.name}': always_run references '{name}' before it is saved (saved in stage '{scenario.stages[j].name}')"
                diagnostics.append(_diag(DiagnosticCode.FORWARD_REF, "warning", msg, location=stage.name))
            else:
                diagnostics.append(
                    _diag(
                        DiagnosticCode.UNDEFINED_VAR,
                        "warning",
                        f"Stage '{stage.name}': always_run references '{name}' — potentially not in scope; only fixtures, "
                        f"parametrize parameters, scenario substitutions, and variables saved by earlier stages are available",
                        location=stage.name,
                    )
                )

        undefined_here: set[str] = set()

        # Stage substitutions resolve strictly in order in the always_run scope
        # (utils.process_substitutions): each step sees only PRIOR steps'
        # names, so their refs are checked cumulatively — checking against the
        # whole pre_iteration set would hide intra-list forward references
        # that are guaranteed TemplatesErrors at runtime. Only ``vars`` values
        # are checked (`raw_substitution_entry_templates`): ``functions``
        # kwargs are never rendered at seed time. A name referenced by several
        # steps is one problem — reported once, at its first occurrence.
        phase_checks: list[tuple[list[str], frozenset[str], bool]] = []
        prior_sub_names: frozenset[str] = frozenset()
        seen_sub_refs: set[str] = set()
        for entry in raw_substitution_entries(raw.get("substitutions")):
            entry_refs = extract_template_variables(raw_substitution_entry_templates(entry)) - seen_sub_refs
            seen_sub_refs |= entry_refs
            phase_checks.append((sorted(entry_refs), scope.always_run | prior_sub_names, True))
            prior_sub_names |= frozenset(raw_substitution_entry_names(entry))
        phase_checks += [
            (sorted(pre_iteration_refs), scope.pre_iteration, True),
            (sorted(request_refs), scope.request, True),
            (sorted(response_refs), scope.response, False),
        ]

        for refs, available, in_request in phase_checks:
            for name in refs:
                if name in available:
                    continue
                if name in scope.stage_substitutions:
                    # Only reachable from the substitution-step checks: in every
                    # later phase the stage's substitution names are in scope.
                    diagnostics.append(
                        _diag(
                            DiagnosticCode.FORWARD_REF,
                            "warning",
                            f"Stage '{stage.name}': substitution references '{name}' before the substitution step that defines it — steps resolve in order",
                            location=stage.name,
                        )
                    )
                elif name in all_saved:
                    j = first_save_stage[name]
                    if j == i and in_request:
                        msg = f"Stage '{stage.name}': variable '{name}' is referenced in the request but only saved in this stage's response"
                    else:
                        msg = f"Stage '{stage.name}': variable '{name}' is referenced before it is saved (saved in stage '{scenario.stages[j].name}')"
                    diagnostics.append(_diag(DiagnosticCode.FORWARD_REF, "warning", msg, location=stage.name))
                else:
                    undefined_here.add(name)

        if undefined_here:
            diagnostics.append(
                _diag(
                    DiagnosticCode.UNDEFINED_VAR,
                    "warning",
                    f"Stage '{stage.name}': potentially undefined variable(s) referenced: {sorted(undefined_here)}",
                    location=stage.name,
                )
            )

    return diagnostics


def _inline_schema_diagnostics(scenario: Scenario) -> list[Diagnostic]:
    """HTTPCHAIN028: scenario reference directives inside an inline JSON Schema.

    Inline ``verify.body.schema`` subtrees are opaque to the reference
    resolver — their ``$ref`` belongs to the schema validator — so an
    ``$include``/``$merge`` found inside one can never be processed, and a
    file-path ``$ref`` can never resolve at runtime either. Both are almost
    certainly leftovers assuming the pre-0.12 behavior of resolving scenario
    directives there.
    """
    diagnostics: list[Diagnostic] = []

    def directive_keys(node: Any) -> set[str]:
        # A leftover directive's value is always a string (anything else was a
        # load error pre-0.12), so string-typed values only: a schema whose
        # `properties` legitimately DECLARES an "$include" property maps it to
        # a schema object (a dict) and is not flagged. "$ref" is normally
        # schema vocabulary and stays unflagged — except a non-"#" value (a
        # file path), which the runtime jsonschema validator can never resolve
        # (no retrieve/base URI is configured) and which was exactly the
        # pre-0.12 way to share an inline schema.
        match node:
            case dict():
                found = set()
                for key, value in node.items():
                    if key in ("$include", "$merge") and isinstance(value, str):
                        found.add(key)
                    elif key == "$ref" and isinstance(value, str) and not value.startswith("#"):
                        found.add(key)
                    found |= directive_keys(value)
                return found
            case list():
                found = set()
                for item in node:
                    found |= directive_keys(item)
                return found
            case _:
                return set()

    for stage in scenario.stages:
        for step in stage.response:
            if isinstance(step, VerifyStep) and isinstance(step.verify.body.schema, dict):
                found = directive_keys(step.verify.body.schema)
                if found:
                    diagnostics.append(
                        _diag(
                            DiagnosticCode.SCHEMA_SCENARIO_DIRECTIVE,
                            "warning",
                            f"Inline JSON schema contains scenario reference directive(s) {sorted(found)}. "
                            f"Inline schemas are standard JSON Schema: scenario directives are not resolved there. "
                            f"Inline the shared content, or reference the schema by file path instead.",
                            location=stage.name,
                        )
                    )
    return diagnostics


def _verify_diagnostics(scenario: Scenario) -> list[Diagnostic]:
    """Per-stage verify-step checks: missing validation, no-op verifies, and
    contradictory body ``contains``/``matches`` declarations.

    Extracted from `check_scenario` so each check family has its own
    helper, mirroring `_dataflow_diagnostics` and the deep-check helpers.
    Behavior, codes and messages are unchanged.
    """
    diagnostics: list[Diagnostic] = []

    for i, stage in enumerate(scenario.stages):
        if not any(isinstance(step, VerifyStep) for step in stage.response):
            diagnostics.append(
                _diag(
                    DiagnosticCode.NO_VERIFY,
                    "warning",
                    f"Stage '{stage.name}' has no response validation (no verify step)",
                    location=stage.name,
                )
            )

        for k, step in enumerate(stage.response):
            if not isinstance(step, VerifyStep):
                continue
            verify = step.verify
            location = f"stages[{i}].response[{k}].verify"

            if _is_noop_verify(verify):
                diagnostics.append(
                    _diag(
                        DiagnosticCode.NOOP_VERIFY,
                        "warning",
                        f"Stage '{stage.name}': verify step asserts nothing (no status, headers, expressions, user functions, or body checks)",
                        location=location,
                    )
                )

            # A verify expression is meant to be a complete ``{{ }}`` template that
            # evaluates to a truthy/falsy value. A plain string (e.g. a forgotten
            # ``{{ }}``) is non-empty and therefore always truthy at runtime, so the
            # assertion silently passes — it tests nothing.
            for expr in verify.expressions:
                if isinstance(expr, str) and not is_complete_template(expr):
                    diagnostics.append(
                        _diag(
                            DiagnosticCode.NONTEMPLATE_EXPRESSION,
                            "warning",
                            f"Stage '{stage.name}': verify expression {expr!r} is not a template ({{{{ }}}}); it is always truthy and asserts nothing",
                            location=location,
                        )
                    )

            diagnostics += _contradiction_diagnostics(
                stage.name,
                "body verification",
                f"{location}.body",
                contains=verify.body.contains,
                not_contains=verify.body.not_contains,
                matches=verify.body.matches,
                not_matches=verify.body.not_matches,
            )

            # Same contradiction rule for header matchers (per header key).
            for header_name, expected in verify.headers.items():
                if not isinstance(expected, HeaderMatcher):
                    continue
                diagnostics += _contradiction_diagnostics(
                    stage.name,
                    f"header '{header_name}' verification",
                    f"{location}.headers.{header_name}",
                    contains=_as_list(expected.contains),
                    not_contains=_as_list(expected.not_contains),
                    matches=_as_list(expected.matches),
                    not_matches=_as_list(expected.not_matches),
                )

    return diagnostics


def _as_list(value: Any) -> list[Any]:
    """None -> [], anything else -> [value]. Adapts HeaderMatcher's optional
    single-value fields to the list-based contradiction checks."""
    return [] if value is None else [value]


def _contradiction_diagnostics(
    stage_name: str,
    what: str,
    location: str,
    *,
    contains: list[Any],
    not_contains: list[Any],
    matches: list[Any],
    not_matches: list[Any],
) -> list[Diagnostic]:
    """The single encoding of the contains/matches contradiction rule, shared
    by body verification and header matchers.

    Overlap is compared on the raw (unrendered) strings: identical entries —
    including identical templates — are caught. A contradiction that only
    emerges after rendering (e.g. a template that resolves to a literal listed
    in the opposite set) is intentionally not pursued, since rendering with a
    partial static context risks false-positive errors."""
    diagnostics: list[Diagnostic] = []

    contains_overlap = {str(s) for s in contains} & {str(s) for s in not_contains}
    if contains_overlap:
        diagnostics.append(
            _diag(
                DiagnosticCode.CONTAINS_CONTRADICTION,
                "error",
                f"Stage '{stage_name}': {what} both requires and forbids substring(s): {sorted(contains_overlap)}",
                location=location,
            )
        )

    matches_overlap = {str(p) for p in matches} & {str(p) for p in not_matches}
    if matches_overlap:
        diagnostics.append(
            _diag(
                DiagnosticCode.MATCHES_CONTRADICTION,
                "error",
                f"Stage '{stage_name}': {what} both requires and forbids pattern(s): {sorted(matches_overlap)}",
                location=location,
            )
        )

    return diagnostics


def _marker_diagnostics(scenario: Scenario) -> list[Diagnostic]:
    """Validate scenario- and stage-level pytest marker expressions.

    Markers are parsed by ``make_marker`` only at collection time, so a malformed
    marker (``skip(``) or an unsupported form (``foo.bar``) would pass ``validate``
    yet crash collection. Run the same parser here so the validator catches it as
    an error, keeping ``validate`` a faithful pre-flight check of what collection
    will accept.
    """
    diagnostics: list[Diagnostic] = []

    def _check(marks: list[str], location: str) -> None:
        for mark in marks:
            try:
                # We only care whether the marker parses; constructing it would
                # otherwise emit PytestUnknownMarkWarning for custom (unregistered)
                # marks, which is noise during validation.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    make_marker(mark)
            except (ValueError, SyntaxError) as e:
                diagnostics.append(
                    _diag(
                        DiagnosticCode.INVALID_MARKER,
                        "error",
                        f"Invalid marker {mark!r}: {e}",
                        location=location,
                    )
                )

    _check(scenario.marks, "marks")
    for i, stage in enumerate(scenario.stages):
        _check(stage.marks, f"stages[{i}].marks")

    return diagnostics


# --------------------------------------------------------------------------- #
# Deep (opt-in) validation: imports, signatures, referenced files.
# These touch the filesystem and import user code, so they NEVER run at
# collection time — only via `validate --deep`. Every finding is a warning.
# --------------------------------------------------------------------------- #


def _literal_path(value: Any) -> Path | None:
    """Return a concrete filesystem path for a literal path value, or None.

    None is returned for missing values, inline schemas (dicts), and any value
    containing a ``{{ }}`` template (resolved at runtime, not statically)."""
    if value is None or isinstance(value, dict):
        return None
    if isinstance(value, Path):
        return None if "{{" in str(value) else value
    if isinstance(value, str):
        return None if "{{" in value else Path(value)
    return None


def _resolve_against(path: Path, base_dir: Path | None) -> Path:
    """Resolve a relative dialect path against the scenario file's directory.

    Mirrors the runtime rule (``Carrier._resolve_scenario_path``): relative
    paths are scenario-file-relative — matching ``$ref`` — not CWD-relative.
    ``base_dir=None`` (no scenario file context) falls back to CWD behavior.
    """
    if path.is_absolute() or base_dir is None:
        return path
    return base_dir / path


def _check_path_value(value: Any, location: str, base_dir: Path | None = None) -> list[Diagnostic]:
    """Existence check for a literal path (or tuple/list of them)."""
    if isinstance(value, tuple | list):
        out: list[Diagnostic] = []
        for idx, item in enumerate(value):
            out += _check_path_value(item, f"{location}[{idx}]", base_dir)
        return out
    path = _literal_path(value)
    if path is not None and not _resolve_against(path, base_dir).exists():
        return [_diag(DiagnosticCode.REFERENCED_FILE_NOT_FOUND, "warning", f"Referenced file not found: {path}", location)]
    return []


def _check_schema_path(schema: Any, location: str, base_dir: Path | None = None) -> list[Diagnostic]:
    """Existence + validity check for a literal JSON-schema file path."""
    path = _literal_path(schema)
    if path is None:
        return []
    path = _resolve_against(path, base_dir)
    if not path.exists():
        return [_diag(DiagnosticCode.REFERENCED_FILE_NOT_FOUND, "warning", f"Schema file not found: {path}", location)]
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return [_diag(DiagnosticCode.SCHEMA_FILE_INVALID, "warning", f"Schema file is not valid JSON: {path}: {e}", location)]
    try:
        check_json_schema(data)
    except Exception as e:
        return [_diag(DiagnosticCode.SCHEMA_FILE_INVALID, "warning", f"Schema file is not a valid JSON Schema: {path}: {e}", location)]
    return []


def _file_diagnostics(scenario: Scenario, base_dir: Path | None = None) -> list[Diagnostic]:
    """Check every literal filesystem path referenced by the scenario.

    ``base_dir`` is the scenario file's directory: relative paths resolve
    against it, matching the runtime behavior."""
    diagnostics: list[Diagnostic] = []
    diagnostics += _check_path_value(scenario.ssl.cert, "ssl.cert", base_dir)
    diagnostics += _check_path_value(scenario.ssl.verify, "ssl.verify", base_dir)

    for i, stage in enumerate(scenario.stages):
        match stage.request.body:
            case BinaryBody(binary=binary):
                diagnostics += _check_path_value(binary, f"stages[{i}].request.body.binary", base_dir)
            case FilesBody(files=files):
                for field, file_path in files.items():
                    diagnostics += _check_path_value(file_path, f"stages[{i}].request.body.files.{field}", base_dir)
            case _:
                pass  # other body types (and no body) carry no filesystem paths

        for k, step in enumerate(stage.response):
            if isinstance(step, VerifyStep):
                diagnostics += _check_schema_path(step.verify.body.schema, f"stages[{i}].response[{k}].verify.body.schema", base_dir)

    return diagnostics


def _func_name_and_kwargs(call: UserFunctionCall) -> tuple[str | None, dict[str, Any] | None]:
    """Extract ``(import_name, explicit_kwargs)`` from a UserFunctionCall.

    ``kwargs`` is None for the bare ``UserFunctionName`` form (no explicit
    keyword arguments). Mirrors the structural dispatch in ``carrier``/``utils`` so
    a new union variant is caught by a class-name search."""
    match call:
        case UserFunctionName():
            return str(call.root), None
        case UserFunctionKwargs():
            return str(call.name.root), dict(call.kwargs or {})
        case _:
            return None, None


def _signature_problems(func: Any, provided: set[str]) -> list[tuple[str, str]]:
    """Compare the names supplied to a call against the function signature.

    ``provided`` is the full set of argument names the runtime supplies (explicit
    kwargs plus framework-injected names such as ``response``). All arguments are
    passed by keyword at the call sites, so positional-only parameters are not
    considered fillable here. Returns ``(code, message)`` tuples."""
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return []  # not introspectable (some builtins/C functions)

    params = list(signature.parameters.values())
    accepts_var_keyword = any(p.kind is p.VAR_KEYWORD for p in params)
    keyword_acceptable = {p.name for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    # Call sites pass everything by keyword (and nothing positionally), so a
    # required POSITIONAL_ONLY param can never be filled — treat it as required
    # too. (It stays out of keyword_acceptable, so passing it by keyword is also
    # correctly reported as unexpected.)
    required = {p.name for p in params if p.default is p.empty and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY, p.POSITIONAL_ONLY)}

    problems: list[tuple[str, str]] = []
    if not accepts_var_keyword:
        for name in sorted(provided - keyword_acceptable):
            problems.append((DiagnosticCode.UNKNOWN_ARG, f"unexpected argument '{name}'"))
    for name in sorted(required - provided):
        problems.append((DiagnosticCode.MISSING_ARG, f"missing required argument '{name}'"))
    return problems


def _function_diagnostics(scenario: Scenario) -> list[Diagnostic]:
    """Resolve every literal user-function reference and (where the call
    arguments are statically known) check them against the signature."""
    diagnostics: list[Diagnostic] = []

    # (call, injected-arg-names, check_signature, location). Substitution
    # ``functions`` are invoked dynamically from templates with unknown call-time
    # arguments, so they are import-checked only.
    sites: list[tuple[UserFunctionCall, set[str], bool, str]] = []

    def _substitution_sites(substitutions: list[Any], location_prefix: str) -> None:
        for sub in substitutions:
            match sub:
                case FunctionsSubstitution(functions=functions):
                    for alias, call in functions.items():
                        sites.append((call, set(), False, f"{location_prefix}.functions.{alias}"))

    if scenario.auth is not None:
        sites.append((scenario.auth, set(), True, "auth"))
    _substitution_sites(scenario.substitutions, "substitutions")

    for i, stage in enumerate(scenario.stages):
        if stage.request.auth is not None:
            sites.append((stage.request.auth, set(), True, f"stages[{i}].request.auth"))
        _substitution_sites(stage.substitutions, f"stages[{i}].substitutions")
        for k, step in enumerate(stage.response):
            match step:
                case SaveStep(save=UserFunctionsSave(user_functions=calls)):
                    for j, call in enumerate(calls):
                        sites.append((call, {"response"}, True, f"stages[{i}].response[{k}].save.user_functions[{j}]"))
                case VerifyStep(verify=verify):
                    for j, call in enumerate(verify.user_functions):
                        sites.append((call, {"response"}, True, f"stages[{i}].response[{k}].verify.user_functions[{j}]"))

    for call, injected, check_signature, location in sites:
        name, kwargs = _func_name_and_kwargs(call)
        if name is None or "{{" in name:
            continue  # template or unrecognized form — not statically resolvable
        try:
            func = import_function(name)
        except UserFunctionError as e:
            diagnostics.append(_diag(DiagnosticCode.IMPORT_FAILED, "warning", f"Cannot import function '{name}': {e}", location))
            continue
        if not check_signature:
            continue
        provided = set(injected) | set((kwargs or {}).keys())
        for code, problem in _signature_problems(func, provided):
            diagnostics.append(_diag(code, "warning", f"Function '{name}': {problem}", location))

    return diagnostics


def check_scenario_deep(scenario: Scenario, syspaths: list[Path] | None = None, scenario_dir: Path | None = None) -> list[Diagnostic]:
    """Opt-in deep checks: referenced-file existence, user-function import
    resolution, and call-signature compatibility.

    Imports user modules (their top-level code runs), so this is only invoked by
    ``validate --deep`` — never at collection time. ``syspaths`` (and the current
    working directory) are temporarily prepended to ``sys.path`` so user modules
    resolve the same way they would under pytest. ``scenario_dir`` is the base
    for relative referenced-file paths, matching the runtime rule."""
    diagnostics = _file_diagnostics(scenario, scenario_dir)

    saved_path = list(sys.path)
    try:
        for entry in [*(str(Path(p).resolve()) for p in (syspaths or [])), str(Path.cwd())]:
            if entry not in sys.path:
                sys.path.insert(0, entry)
        diagnostics += _function_diagnostics(scenario)
    finally:
        sys.path[:] = saved_path

    return diagnostics


class ScenarioInfo(BaseModel):
    """Detailed information about the scenario structure."""

    num_stages: int = 0
    stage_names: list[str] = []
    vars_referenced: list[str] = []
    vars_saved: list[str] = []
    vars_defined: list[str] = []
    fixtures: list[str] = []


class ValidateResult(BaseModel):
    """Result of scenario validation."""

    valid: bool
    errors: list[str] = []
    warnings: list[str] = []
    diagnostics: list[Diagnostic] = []
    scenario_info: ScenarioInfo | None = None


def _result(diagnostics: list[Diagnostic], scenario_info: ScenarioInfo | None = None) -> ValidateResult:
    errors = [d.message for d in diagnostics if d.severity == "error"]
    warnings = [d.message for d in diagnostics if d.severity == "warning"]
    return ValidateResult(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        diagnostics=diagnostics,
        scenario_info=scenario_info,
    )


def check_scenario(scenario: Scenario, test_data: dict[str, Any]) -> tuple[list[Diagnostic], ScenarioInfo]:
    """Semantic checks on an already-loaded, schema-valid scenario.

    Operates on the parsed ``test_data`` dict together with the validated ``Scenario``
    so it can be shared by `validate_scenario` (file-based) and by the pytest
    collector. Returns ``(diagnostics, scenario_info)``.
    """
    diagnostics: list[Diagnostic] = []

    stage_names = [stage.name for stage in scenario.stages]
    seen_names: set[str] = set()
    duplicate_names: set[str] = set()
    for name in stage_names:
        if name in seen_names:
            duplicate_names.add(name)
        seen_names.add(name)
    if duplicate_names:
        diagnostics.append(
            _diag(
                DiagnosticCode.DUPLICATE_STAGE,
                "error",
                f"Duplicate stage names found: {sorted(duplicate_names)}",
                location="stages",
            )
        )

    fixtures: list[str] = list(scenario.fixtures)
    for stage in scenario.stages:
        fixtures.extend(stage.fixtures)
    fixtures = sorted(set(fixtures))

    vars_defined = extract_defined_variables(scenario)
    vars_saved = extract_saved_variables(scenario)
    vars_referenced = extract_template_variables(test_data)

    # HTTPCHAIN002: a fixture and a variable (substitution or parametrize/foreach
    # parameter) that share a name AND coexist in the same stage conflict — the
    # fixture shadows the variable in that stage's generated method. Scope the check
    # per stage so a fixture used only in one stage and a same-named parameter used
    # only in another (which never coexist) are not falsely flagged.
    scenario_sub_names = set(substitution_names(scenario.substitutions))
    var_conflicts: set[str] = set()
    for scope in stage_scopes(scenario):
        fixtures_in_stage = scope.scenario_fixtures | scope.stage_fixtures
        vars_in_stage = scope.scenario_substitutions | scope.stage_substitutions | scope.parametrize_params | scope.foreach_params
        var_conflicts |= fixtures_in_stage & vars_in_stage
    if var_conflicts:
        diagnostics.append(
            _diag(
                DiagnosticCode.FIXTURE_CONFLICT,
                "error",
                f"Conflicting fixtures and vars with same names: {sorted(var_conflicts)}",
            )
        )

    # Scenario fixtures are injected into every stage and sit above the global
    # context in the runtime ChainMap, so a save under the same name can never
    # be read back — the fixture value always wins.
    shadowed_saves = set(scenario.fixtures) & vars_saved
    if shadowed_saves:
        diagnostics.append(
            _diag(
                DiagnosticCode.FIXTURE_SHADOWS_SAVE,
                "warning",
                f"Saved variables shadowed by scenario-level fixtures: {sorted(shadowed_saves)} (fixture values win in every stage; these saves can never be read)",
            )
        )

    # Scenario-level templates (substitutions/auth/ssl) resolve once per
    # scenario (carrier's lazy initialization, or at collection when stage
    # parametrize values reference them) against a context that deliberately
    # excludes fixture values — a fixture reference there is a guaranteed crash.
    for key in ("substitutions", "auth", "ssl"):
        scenario_level_refs = extract_template_variables(test_data.get(key))
        fixture_refs = scenario_level_refs & set(fixtures)
        if fixture_refs:
            diagnostics.append(
                _diag(
                    DiagnosticCode.FIXTURE_IN_SCENARIO_TEMPLATE,
                    "error",
                    f"Fixtures referenced in scenario-level '{key}' templates: {sorted(fixture_refs)} (the scenario-level context never includes fixture values)",
                    location=key,
                )
            )
        # Beyond fixtures, scenario-level templates resolve against only the
        # scenario substitutions (no stage vars, no saved values). A reference
        # to anything else is a guaranteed crash at scenario initialization, so
        # flag it as an error (fixtures are reported separately above).
        undefined_refs = scenario_level_refs - set(fixtures) - scenario_sub_names
        if undefined_refs:
            diagnostics.append(
                _diag(
                    DiagnosticCode.SCENARIO_UNDEFINED_VAR,
                    "error",
                    f"Undefined variable(s) in scenario-level '{key}' templates: {sorted(undefined_refs)} (resolved against only scenario substitutions, before any stage runs)",
                    location=key,
                )
            )

    # Response METADATA (status/reason/headers/elapsed_ms) is ambient in
    # response-step templates as the reserved `response` namespace — encoded in
    # scoping.StageScopes.response, so the data-flow check accepts it there and
    # only there. A user-defined name colliding with it is shadowed inside
    # response steps: warn rather than let the save silently change meaning.
    reserved_conflicts = (vars_defined | vars_saved | set(fixtures)) & {RESPONSE_META_NAME}
    if reserved_conflicts:
        diagnostics.append(
            _diag(
                DiagnosticCode.RESERVED_NAME,
                "warning",
                f"Name(s) {sorted(reserved_conflicts)} are shadowed by the reserved response metadata namespace inside response steps "
                f"(save/verify templates see the HTTP response there, not your value)",
            )
        )

    diagnostics.extend(_dataflow_diagnostics(scenario, test_data))

    diagnostics.extend(_verify_diagnostics(scenario))
    diagnostics.extend(_inline_schema_diagnostics(scenario))

    diagnostics.extend(_marker_diagnostics(scenario))

    # Phase visibility: template-bearing parametrize VALUES opt the scenario into
    # collection-time resolution of scenario substitutions (pytest needs concrete
    # parameter values to generate test items) — the one exception to lazy,
    # execution-time scenario initialization. Surface that as an info diagnostic
    # so the timing is discoverable exactly when a scenario triggers it. Uses the
    # same predicate as the carrier, so validator and runtime agree by construction.
    for i, stage in enumerate(scenario.stages):
        if parametrize_values_contain_template(stage.parametrize):
            diagnostics.append(
                _diag(
                    DiagnosticCode.PARAMETRIZE_COLLECTION_RESOLUTION,
                    "info",
                    f"Stage '{stage.name}' has template parametrize values: scenario-level substitutions for this scenario "
                    f"resolve at collection time (pytest needs concrete parameter values), including any user functions they call",
                    location=f"stages[{i}].parametrize",
                )
            )

    scenario_info = ScenarioInfo(
        num_stages=len(scenario.stages),
        stage_names=stage_names,
        vars_referenced=sorted(vars_referenced),
        vars_saved=sorted(vars_saved),
        vars_defined=sorted(vars_defined),
        fixtures=fixtures,
    )

    return diagnostics, scenario_info


# Standard project-root markers, matching the files pytest's own rootdir
# discovery recognizes (plus .git as the universal repository marker).
_ROOT_MARKERS = ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg", "setup.py", ".git")


def resolve_root_path(path: Path) -> Path:
    """Directory that constrains ``$ref`` resolution when no explicit root is
    given: the nearest ancestor of ``path`` that looks like a project root
    (contains one of the standard project markers); in a marker-less tree
    (e.g. an exported scenario bundle), the nearest ``tests/`` ancestor, else
    the file's own parent.

    The marker walk approximates pytest's ``rootdir``, so the CLI sandboxes
    ``$ref`` resolution the same way pytest collection does (collection passes
    ``config.rootpath`` explicitly); the ``tests/`` fallback preserves the
    pre-marker default so marker-less trees keep their sandbox breadth."""
    for ancestor in path.resolve().parents:
        if any((ancestor / marker).exists() for marker in _ROOT_MARKERS):
            return ancestor
    for ancestor in path.resolve().parents:
        if ancestor.name == "tests":
            return ancestor
    return path.parent


def is_inline_schema_position(path: tuple[str | int, ...]) -> bool:
    """True for raw-JSON positions that hold an inline verify schema.

    These subtrees are standard JSON Schema — their ``$ref``/``$defs`` are
    addressed to the schema validator, not the scenario's reference resolver —
    so the load pipeline passes this as the resolver's ``opaque`` predicate.

    The grammar (mirroring the ``Scenario`` model) is
    ``stages[K].response[K].verify.body.schema``, where stages and response
    steps each accept both the list form (``K`` is an index) and the
    name-keyed mapping form (``K`` is a name), and a response mapping value
    may itself be a list of steps (one extra index segment).
    """
    if path[-3:] != ("verify", "body", "schema"):
        return False
    head = path[:-3]
    if len(head) == 4:
        return head[0] == "stages" and head[2] == "response"
    if len(head) == 5:
        return head[0] == "stages" and head[2] == "response" and isinstance(head[4], int)
    return False


def load_scenario(path: Path, *, root_path: Path | None = None, ref_parent_traversal_depth: int = 3) -> tuple[Scenario, dict[str, Any]]:
    """Load, ``$ref``-resolve and validate a scenario file -> ``(scenario, raw_data)``.

    The single load+resolve+validate path shared by pytest collection
    (``plugin.py:JsonModule.collect``, which passes ``config.rootpath``) and the
    CLI commands. ``root_path`` constrains ``$ref`` resolution; when omitted it
    defaults to `resolve_root_path` (the auto-detected project root, which
    approximates ``config.rootpath``). Inline verify schemas are opaque to the
    resolver (`is_inline_schema_position`). Raises ``ReferenceResolverError`` /
    ``json.JSONDecodeError`` / ``pydantic.ValidationError`` on failure; callers
    map these to user-facing errors.
    """
    if root_path is None:
        root_path = resolve_root_path(path)
    test_data = load_json(path, max_parent_traversal_depth=ref_parent_traversal_depth, root_path=root_path, opaque=is_inline_schema_position)
    return Scenario.model_validate(test_data), test_data


def validate_scenario(
    path: Path,
    ref_parent_traversal_depth: int = 3,
    root_path: Path | None = None,
    deep: bool = False,
    syspaths: list[Path] | None = None,
) -> ValidateResult:
    """Validate a pytest-httpchain test scenario file.

    Performs file/JSON/$ref/schema validation plus the semantic checks in
    `check_scenario`, returning a `ValidateResult` whose
    ``diagnostics`` carry stable codes.

    When ``deep`` is true, additionally runs `check_scenario_deep`
    (referenced-file, import, and signature checks). This imports user modules,
    so it is opt-in; ``syspaths`` are added to ``sys.path`` for resolution.
    """
    diagnostics: list[Diagnostic] = []

    if not path.exists():
        return _result([_diag(DiagnosticCode.FILE_NOT_FOUND, "error", f"File not found: {path}")])

    if not path.is_file():
        return _result([_diag(DiagnosticCode.NOT_A_FILE, "error", f"Path is not a file: {path}")])

    if path.suffix.lower() != ".json":
        diagnostics.append(
            _diag(
                DiagnosticCode.WRONG_EXTENSION,
                "warning",
                f"File has extension '{path.suffix}' but expected '.json'. Consider renaming to use .json extension.",
                location=str(path),
            )
        )

    # Record ambiguity warnings from the $ref resolver (HTTPCHAIN026) instead
    # of letting them escape as bare Python warnings; anything else recorded
    # is re-emitted unchanged after the block. Load errors are collected, not
    # returned from inside the block, so warnings already earned by earlier
    # (successful) references are reported even when a later one fails.
    load_failed = False
    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        try:
            scenario, test_data = load_scenario(path, root_path=root_path, ref_parent_traversal_depth=ref_parent_traversal_depth)
        except ReferenceResolverError as e:
            # The resolver wraps a plain JSON syntax error as a ReferenceResolverError
            # (chaining the JSONDecodeError as __cause__), and raises DuplicateKeyError
            # for a duplicated object key. Both are JSON *content* problems — report
            # them under the accurate "Invalid JSON syntax" code rather than the
            # $ref-flavored one, which would mislead when no reference is involved.
            if isinstance(e, DuplicateKeyError):
                diagnostics.append(_diag(DiagnosticCode.INVALID_JSON, "error", f"Invalid JSON: {e}"))
            elif isinstance(e.__cause__, json.JSONDecodeError):
                diagnostics.append(_diag(DiagnosticCode.INVALID_JSON, "error", f"Invalid JSON syntax: {e.__cause__}"))
            else:
                diagnostics.append(_diag(DiagnosticCode.REF_ERROR, "error", f"JSON reference resolution error: {e}"))
            load_failed = True
        except json.JSONDecodeError as e:
            diagnostics.append(_diag(DiagnosticCode.INVALID_JSON, "error", f"Invalid JSON syntax: {e}"))
            load_failed = True
        except ValidationError as e:
            for err in e.errors():
                loc = " -> ".join(str(x) for x in err["loc"])
                diagnostics.append(_diag(DiagnosticCode.SCHEMA, "error", f"Schema validation failed: {loc}: {err['msg']}", location=loc))
            load_failed = True
        except Exception as e:
            diagnostics.append(_diag(DiagnosticCode.PARSE_ERROR, "error", f"Failed to parse JSON file: {e}"))
            load_failed = True

    for caught in caught_warnings:
        if isinstance(caught.message, AmbiguousReferenceWarning):
            diagnostics.append(_diag(DiagnosticCode.AMBIGUOUS_REF, "warning", str(caught.message)))
        else:
            warnings.warn_explicit(caught.message, caught.category, caught.filename, caught.lineno)

    if load_failed:
        return _result(diagnostics)

    semantic_diagnostics, scenario_info = check_scenario(scenario, test_data)
    diagnostics.extend(semantic_diagnostics)

    if deep:
        diagnostics.extend(check_scenario_deep(scenario, syspaths=syspaths, scenario_dir=path.parent))

    return _result(diagnostics, scenario_info)
