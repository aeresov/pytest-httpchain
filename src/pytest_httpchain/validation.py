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
004      warning  Variable referenced before it is saved (ordering / data-flow)
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
======== ======== ==========================================================

The ``02x`` codes come from *deep* validation, which is opt-in (``validate
--deep``) because it imports user modules and touches the filesystem; it never
runs at collection time. Deep findings are always warnings.
"""

import ast
import inspect
import json
import re
import sys
import warnings
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError
from pytest_httpchain_templates import TEMPLATE_BUILTINS, TEMPLATE_PATTERN, is_complete_template
from pytest_httpchain_userfunc import UserFunctionError, import_function

from pytest_httpchain.jsonref import ReferenceResolverError, load_json
from pytest_httpchain.models import (
    FunctionsSubstitution,
    JMESPathSave,
    Parameters,
    SaveStep,
    Scenario,
    Stage,
    SubstitutionsSave,
    UserFunctionCall,
    UserFunctionKwargs,
    UserFunctionName,
    UserFunctionsSave,
    VarsSubstitution,
    Verify,
    VerifyStep,
    check_json_schema,
)
from pytest_httpchain.utils import make_marker

Severity = Literal["error", "warning"]


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


class Diagnostic(BaseModel):
    """A single validation finding."""

    code: str
    severity: Severity
    message: str
    location: str | None = None


def _diag(code: str, severity: Severity, message: str, location: str | None = None) -> Diagnostic:
    return Diagnostic(code=code, severity=severity, message=message, location=location)


def _extract_names_from_expr(expr: str) -> set[str]:
    """Extract free identifier names referenced by a Python expression.

    Names bound *within* the expression — comprehension targets
    (``for x in ...``) and lambda parameters — are local bindings, not context
    references, so they are excluded. Falls back to a permissive regex if the
    expression doesn't parse.
    """
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError:
        return set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", expr))

    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.comprehension):
            bound |= {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.Lambda):
            a = node.args
            for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg):
                if arg is not None:
                    bound.add(arg.arg)

    loaded = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    return loaded - bound


def extract_template_variables(obj: Any, variables: set[str] | None = None) -> set[str]:
    """Recursively extract variable names from {{ expr }} template expressions."""
    if variables is None:
        variables = set()

    if isinstance(obj, str):
        for match in re.finditer(TEMPLATE_PATTERN, obj):
            names = _extract_names_from_expr(match.group("expr"))
            variables.update(names - TEMPLATE_BUILTINS)
    elif isinstance(obj, dict):
        for value in obj.values():
            extract_template_variables(value, variables)
    elif isinstance(obj, list):
        for item in obj:
            extract_template_variables(item, variables)

    return variables


def substitution_names(substitutions: Any) -> set[str]:
    """Names introduced by a list of ``vars``/``functions`` substitution entries."""
    names: set[str] = set()
    for sub in substitutions or []:
        match sub:
            case VarsSubstitution():
                names.update(sub.vars.keys())
            case FunctionsSubstitution():
                names.update(sub.functions.keys())
    return names


def saved_in_stage(stage: Stage) -> set[str]:
    """Variable names a single stage's response steps save into the context."""
    saved: set[str] = set()
    for response_step in stage.response:
        if not isinstance(response_step, SaveStep):
            continue
        match response_step.save:
            case JMESPathSave(jmespath=jmespath):
                saved.update(jmespath.keys())
            case SubstitutionsSave(substitutions=substitutions):
                saved |= substitution_names(substitutions)
            case UserFunctionsSave():
                # user_functions saves return arbitrary dict keys -> not statically known.
                pass
    return saved


def extract_saved_variables(scenario: Scenario) -> set[str]:
    """Extract variable names saved across all response steps in the scenario."""
    saved_vars: set[str] = set()
    for stage in scenario.stages:
        saved_vars |= saved_in_stage(stage)
    return saved_vars


def parameter_names(params: Parameters | None) -> set[str]:
    """Names injected by a list of parametrize/foreach Parameter entries.

    Covers both ``individual`` (one name -> list of values) and ``combinations``
    (list of dicts whose keys are the names). Template-string forms (deferred to
    runtime) contribute no statically-known names.
    """
    names: set[str] = set()
    for param in params or []:
        individual = getattr(param, "individual", None)
        if isinstance(individual, dict):
            names.update(k for k in individual if isinstance(k, str))
        combinations = getattr(param, "combinations", None)
        if isinstance(combinations, list):
            for combo in combinations:
                if isinstance(combo, dict):
                    names.update(k for k in combo if isinstance(k, str))
    return names


def stage_defined_names(stage: Stage) -> set[str]:
    """Names available *within a single stage*: its substitutions, parametrize /
    foreach parameters, and its declared fixtures."""
    names = substitution_names(stage.substitutions)
    names |= parameter_names(stage.parametrize)
    if stage.parallel is not None:
        names |= parameter_names(getattr(stage.parallel, "foreach", None))
    names |= set(stage.fixtures)
    return names


def extract_defined_variables(scenario: Scenario) -> set[str]:
    """Extract variable names made available before/within templates (scenario-wide).

    Sources: ``vars`` and ``functions`` substitutions (scenario- and stage-level),
    plus parameter names injected by ``parametrize`` and ``parallel.foreach``. This
    is the *union* across the whole scenario, used for the fixture-conflict check
    and informational output; the order-aware data-flow check
    (`_dataflow_diagnostics`) computes availability per stage instead.
    """
    defined_vars: set[str] = set()

    defined_vars |= substitution_names(scenario.substitutions)

    for stage in scenario.stages:
        defined_vars |= substitution_names(stage.substitutions)
        defined_vars |= parameter_names(stage.parametrize)
        if stage.parallel is not None:
            defined_vars |= parameter_names(getattr(stage.parallel, "foreach", None))

    return defined_vars


def raw_stages(test_data: dict[str, Any]) -> list[Any]:
    """Raw (pre-validation) stage bodies in declaration order.

    Stages may be authored as a list or as a ``{name: stage}`` mapping; both
    preserve order, matching the normalized ``scenario.stages``."""
    raw = test_data.get("stages")
    if isinstance(raw, dict):
        return list(raw.values())
    if isinstance(raw, list):
        return raw
    return []


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

    Walks stages in execution order tracking which variables are available at
    each reference site, mirroring the runtime scoping in ``carrier.py``:

    * scenario-level substitutions and scenario-level fixtures are available
      everywhere;
    * a stage's substitutions and ``parallel`` config resolve at stage start,
      before any iteration: they see fixtures, parametrize parameters, scenario
      substitutions and earlier saves — but not ``foreach`` parameters;
    * a stage's request and response additionally see the ``foreach`` parameters
      (resolved per iteration); intra-step ordering within a stage's response is
      approximated (all of a stage's own saves are treated as available to its
      response), so a verify that reads a value saved by a later save step in the
      same stage is not flagged;
    * a value saved in a stage's response is available to that stage's response
      and to *later* stages — but never to the same stage's request, and never
      to earlier stages;
    * ``always_run`` is evaluated at stage start, before stage substitutions are
      processed and before any iteration runs: only fixtures, parametrize
      parameters, scenario substitutions and earlier saves are in scope.

    A reference that is unavailable is reported as `DiagnosticCode.FORWARD_REF`
    if the name is saved somewhere later (an ordering bug) or
    `DiagnosticCode.UNDEFINED_VAR` otherwise (likely a typo).
    """
    diagnostics: list[Diagnostic] = []

    all_saved = extract_saved_variables(scenario)
    saves_by_stage: list[set[str]] = [saved_in_stage(stage) for stage in scenario.stages]
    first_save_stage: dict[str, int] = {}
    for i, saved in enumerate(saves_by_stage):
        for name in saved:
            first_save_stage.setdefault(name, i)

    # ``parametrize`` parameter VALUES are resolved at collection time
    # (carrier.create_test_class) against scenario-level substitutions only — no
    # fixtures, no stage substitutions, no parameter names, no saved values exist
    # yet. ``scenario_scope`` is that narrow set; ``scenario_available`` is the
    # everywhere-available set used for ordinary request/response references.
    scenario_scope: set[str] = set(substitution_names(scenario.substitutions))

    scenario_available = scenario_scope | set(scenario.fixtures)

    # raws[i] is the unvalidated dict for scenario.stages[i]: both come from the same
    # order-preserving normalization (_normalize_stages_input), so they pair by index.
    raws = raw_stages(test_data)
    cumulative_saves: set[str] = set()

    for i, stage in enumerate(scenario.stages):
        raw = raws[i] if i < len(raws) and isinstance(raws[i], dict) else {}

        # Stage substitutions and the parallel config are resolved at stage start,
        # BEFORE any foreach iteration variable exists, so they see fixtures,
        # parametrize parameters, scenario substitutions and earlier saves — but not
        # foreach parameters. The request/response are resolved per iteration, so
        # they additionally see the foreach parameters.
        foreach_params = parameter_names(getattr(stage.parallel, "foreach", None)) if stage.parallel is not None else set()
        pre_iteration_available = scenario_available | set(stage.fixtures) | parameter_names(stage.parametrize) | substitution_names(stage.substitutions) | cumulative_saves
        request_available = pre_iteration_available | foreach_params
        response_available = request_available | saves_by_stage[i]

        pre_iteration_refs: set[str] = set()
        for key in ("substitutions", "parallel"):
            extract_template_variables(raw.get(key), pre_iteration_refs)
        request_refs = extract_template_variables(raw.get("request"))
        response_refs = extract_template_variables(raw.get("response"))

        for name in sorted(extract_template_variables(raw.get("parametrize"))):
            if name in scenario_scope:
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
        always_run_available = scenario_available | set(stage.fixtures) | parameter_names(stage.parametrize) | cumulative_saves
        for name in sorted(extract_template_variables(raw.get("always_run"))):
            if name in always_run_available:
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

        for refs, available, in_request in (
            (sorted(pre_iteration_refs), pre_iteration_available, True),
            (sorted(request_refs), request_available, True),
            (sorted(response_refs), response_available, False),
        ):
            for name in refs:
                if name in available:
                    continue
                if name in all_saved:
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

        cumulative_saves |= saves_by_stage[i]

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

            # Overlap is compared on the raw (unrendered) strings: identical
            # entries — including identical templates — are caught. A contradiction
            # that only emerges after rendering (e.g. a template that resolves to a
            # literal listed in the opposite set) is intentionally not pursued, since
            # rendering with a partial static context risks false-positive errors.
            contains_overlap = {str(s) for s in verify.body.contains} & {str(s) for s in verify.body.not_contains}
            if contains_overlap:
                diagnostics.append(
                    _diag(
                        DiagnosticCode.CONTAINS_CONTRADICTION,
                        "error",
                        f"Stage '{stage.name}': body verification both requires and forbids substring(s): {sorted(contains_overlap)}",
                        location=f"{location}.body",
                    )
                )

            matches_overlap = {str(p) for p in verify.body.matches} & {str(p) for p in verify.body.not_matches}
            if matches_overlap:
                diagnostics.append(
                    _diag(
                        DiagnosticCode.MATCHES_CONTRADICTION,
                        "error",
                        f"Stage '{stage.name}': body verification both requires and forbids pattern(s): {sorted(matches_overlap)}",
                        location=f"{location}.body",
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


def _check_path_value(value: Any, location: str) -> list[Diagnostic]:
    """Existence check for a literal path (or tuple/list of them)."""
    if isinstance(value, tuple | list):
        out: list[Diagnostic] = []
        for idx, item in enumerate(value):
            out += _check_path_value(item, f"{location}[{idx}]")
        return out
    path = _literal_path(value)
    if path is not None and not path.exists():
        return [_diag(DiagnosticCode.REFERENCED_FILE_NOT_FOUND, "warning", f"Referenced file not found: {path}", location)]
    return []


def _check_schema_path(schema: Any, location: str) -> list[Diagnostic]:
    """Existence + validity check for a literal JSON-schema file path."""
    path = _literal_path(schema)
    if path is None:
        return []
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


def _file_diagnostics(scenario: Scenario) -> list[Diagnostic]:
    """Check every literal filesystem path referenced by the scenario."""
    diagnostics: list[Diagnostic] = []
    diagnostics += _check_path_value(getattr(scenario.ssl, "cert", None), "ssl.cert")

    for i, stage in enumerate(scenario.stages):
        body = stage.request.body
        if body is not None:
            diagnostics += _check_path_value(getattr(body, "binary", None), f"stages[{i}].request.body.binary")
            files = getattr(body, "files", None)
            if isinstance(files, dict):
                for field, file_path in files.items():
                    diagnostics += _check_path_value(file_path, f"stages[{i}].request.body.files.{field}")

        for k, step in enumerate(stage.response):
            verify = getattr(step, "verify", None)
            if verify is not None:
                diagnostics += _check_schema_path(verify.body.schema, f"stages[{i}].response[{k}].verify.body.schema")

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
    sites: list[tuple[Any, set[str], bool, str]] = []

    if scenario.auth is not None:
        sites.append((scenario.auth, set(), True, "auth"))
    for sub in scenario.substitutions:
        functions = getattr(sub, "functions", None)
        if isinstance(functions, dict):
            for alias, call in functions.items():
                sites.append((call, set(), False, f"substitutions.functions.{alias}"))

    for i, stage in enumerate(scenario.stages):
        if stage.request.auth is not None:
            sites.append((stage.request.auth, set(), True, f"stages[{i}].request.auth"))
        for sub in stage.substitutions:
            functions = getattr(sub, "functions", None)
            if isinstance(functions, dict):
                for alias, call in functions.items():
                    sites.append((call, set(), False, f"stages[{i}].substitutions.functions.{alias}"))
        for k, step in enumerate(stage.response):
            save = getattr(step, "save", None)
            if save is not None:
                for j, call in enumerate(getattr(save, "user_functions", []) or []):
                    sites.append((call, {"response"}, True, f"stages[{i}].response[{k}].save.user_functions[{j}]"))
            verify = getattr(step, "verify", None)
            if verify is not None:
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


def check_scenario_deep(scenario: Scenario, syspaths: list[Path] | None = None) -> list[Diagnostic]:
    """Opt-in deep checks: referenced-file existence, user-function import
    resolution, and call-signature compatibility.

    Imports user modules (their top-level code runs), so this is only invoked by
    ``validate --deep`` — never at collection time. ``syspaths`` (and the current
    working directory) are temporarily prepended to ``sys.path`` so user modules
    resolve the same way they would under pytest."""
    diagnostics = _file_diagnostics(scenario)

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
    scenario_fixture_set = set(scenario.fixtures)
    scenario_sub_names = set(substitution_names(scenario.substitutions))
    var_conflicts: set[str] = set()
    for stage in scenario.stages:
        stage_fixtures = scenario_fixture_set | set(stage.fixtures)
        stage_params = parameter_names(stage.parametrize)
        if stage.parallel is not None:
            stage_params |= parameter_names(getattr(stage.parallel, "foreach", None))
        stage_vars = scenario_sub_names | substitution_names(stage.substitutions) | stage_params
        var_conflicts |= stage_fixtures & stage_vars
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

    # Scenario-level templates (substitutions/auth/ssl) resolve once at class
    # creation (carrier.create_test_class), before any fixture exists — a fixture
    # reference there is a guaranteed collection-time crash.
    for key in ("substitutions", "auth", "ssl"):
        scenario_level_refs = extract_template_variables(test_data.get(key))
        fixture_refs = scenario_level_refs & set(fixtures)
        if fixture_refs:
            diagnostics.append(
                _diag(
                    DiagnosticCode.FIXTURE_IN_SCENARIO_TEMPLATE,
                    "error",
                    f"Fixtures referenced in scenario-level '{key}' templates: {sorted(fixture_refs)} (scenario-level templates resolve at collection time, before fixtures exist)",
                    location=key,
                )
            )
        # Beyond fixtures, scenario-level templates resolve at collection against
        # only the scenario substitutions (no stage vars, no saved values). A
        # reference to anything else is a guaranteed collection-time crash, so flag
        # it as an error (fixtures are reported separately above).
        undefined_refs = scenario_level_refs - set(fixtures) - scenario_sub_names
        if undefined_refs:
            diagnostics.append(
                _diag(
                    DiagnosticCode.SCENARIO_UNDEFINED_VAR,
                    "error",
                    f"Undefined variable(s) in scenario-level '{key}' templates: {sorted(undefined_refs)} (these resolve at collection time against only scenario substitutions)",
                    location=key,
                )
            )

    # NOTE: response data (response/status_code/body/json/text/headers/cookies) is
    # NOT ambient in {{ }} templates — it reaches save/verify handlers directly and
    # only enters the template context via an earlier `save` step. So there are no
    # response "builtins" to whitelist here; template functions are already excluded
    # from references via TEMPLATE_BUILTINS.
    diagnostics.extend(_dataflow_diagnostics(scenario, test_data))

    diagnostics.extend(_verify_diagnostics(scenario))

    diagnostics.extend(_marker_diagnostics(scenario))

    scenario_info = ScenarioInfo(
        num_stages=len(scenario.stages),
        stage_names=stage_names,
        vars_referenced=sorted(vars_referenced),
        vars_saved=sorted(vars_saved),
        vars_defined=sorted(vars_defined),
        fixtures=fixtures,
    )

    return diagnostics, scenario_info


def resolve_root_path(path: Path) -> Path:
    """Directory that constrains ``$ref`` resolution: the nearest ``tests/``
    ancestor of ``path``, else the file's own parent."""
    potential_root = path.parent
    while potential_root.parent != potential_root:
        if potential_root.name == "tests":
            return potential_root
        potential_root = potential_root.parent
    return path.parent


def load_scenario(path: Path, *, root_path: Path | None = None, ref_parent_traversal_depth: int = 3) -> tuple[Scenario, dict[str, Any]]:
    """Load, ``$ref``-resolve and validate a scenario file -> ``(scenario, raw_data)``.

    The single load+resolve+validate path shared by the CLI inspection commands
    (``show``/``graph``) and the data-flow loader. ``root_path`` constrains ``$ref``
    resolution; when omitted it defaults to `resolve_root_path` (the nearest
    ``tests/`` ancestor).

    NOTE on the root-path divergence: pytest collection (``plugin.py``) constrains
    ``$ref`` resolution to ``config.rootpath`` (the repo root), while every CLI path
    defaults to `resolve_root_path`. A ``$ref`` reaching above ``tests/`` but
    inside the repo therefore resolves under collection yet may need an explicit
    ``--root-path`` to resolve the same way from the CLI. Raises
    ``ReferenceResolverError`` / ``json.JSONDecodeError`` / ``pydantic.ValidationError``
    on failure; callers map these to user-facing errors.
    """
    if root_path is None:
        root_path = resolve_root_path(path)
    test_data = load_json(path, max_parent_traversal_depth=ref_parent_traversal_depth, root_path=root_path)
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

    if root_path is None:
        root_path = resolve_root_path(path)

    try:
        test_data = load_json(
            path,
            max_parent_traversal_depth=ref_parent_traversal_depth,
            root_path=root_path,
        )
    except ReferenceResolverError as e:
        # The resolver wraps a plain JSON syntax error as a ReferenceResolverError
        # (chaining the JSONDecodeError as __cause__). Report those under the
        # accurate "Invalid JSON syntax" code rather than the $ref-flavored one,
        # which would mislead when no reference is involved.
        if isinstance(e.__cause__, json.JSONDecodeError):
            diagnostics.append(_diag(DiagnosticCode.INVALID_JSON, "error", f"Invalid JSON syntax: {e.__cause__}"))
        else:
            diagnostics.append(_diag(DiagnosticCode.REF_ERROR, "error", f"JSON reference resolution error: {e}"))
        return _result(diagnostics)
    except json.JSONDecodeError as e:
        diagnostics.append(_diag(DiagnosticCode.INVALID_JSON, "error", f"Invalid JSON syntax: {e}"))
        return _result(diagnostics)
    except Exception as e:
        diagnostics.append(_diag(DiagnosticCode.PARSE_ERROR, "error", f"Failed to parse JSON file: {e}"))
        return _result(diagnostics)

    try:
        scenario = Scenario.model_validate(test_data)
    except ValidationError as e:
        for err in e.errors():
            loc = " -> ".join(str(x) for x in err["loc"])
            diagnostics.append(_diag(DiagnosticCode.SCHEMA, "error", f"Schema validation failed: {loc}: {err['msg']}", location=loc))
        return _result(diagnostics)

    semantic_diagnostics, scenario_info = check_scenario(scenario, test_data)
    diagnostics.extend(semantic_diagnostics)

    if deep:
        diagnostics.extend(check_scenario_deep(scenario, syspaths=syspaths))

    return _result(diagnostics, scenario_info)
