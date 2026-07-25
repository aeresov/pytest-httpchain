"""Semantic checks a JSON Schema cannot express, run by both the CLI and pytest collection.

Each check family is a generator of `Diagnostic`; `check_scenario` composes them.
"""

import warnings
from collections import Counter
from collections.abc import Iterator
from typing import Any

from pytest_httpchain.models import HeaderMatcher, Scenario, Verify, VerifyStep, parametrize_values_contain_template
from pytest_httpchain.scoping import (
    RESPONSE_META_NAME,
    SCENARIO_TEMPLATE_FIELDS,
    extract_defined_variables,
    extract_saved_variables,
    extract_template_variables,
    raw_stages,
    stage_scopes,
    substitution_names,
    substitution_step_refs,
)
from pytest_httpchain.templates import is_complete_template
from pytest_httpchain.utils import make_marker, optional_as_list
from pytest_httpchain.validation.diagnostics import Diagnostic, DiagnosticCode, ScenarioInfo, diag


def check_scenario(scenario: Scenario, test_data: dict[str, Any]) -> tuple[list[Diagnostic], ScenarioInfo]:
    """Semantic checks on an already-loaded, schema-valid scenario.

    Takes the parsed ``test_data`` alongside the validated model so template
    references can be read from the raw text. The composition order below is the
    reported order.
    """
    fixtures = sorted({*scenario.fixtures, *(name for stage in scenario.stages for name in stage.fixtures)})
    vars_defined = extract_defined_variables(scenario)
    vars_saved = extract_saved_variables(scenario)
    scenario_sub_names = set(substitution_names(scenario.substitutions))

    diagnostics: list[Diagnostic] = [
        *_stage_name_diagnostics(scenario),
        *_fixture_diagnostics(scenario, vars_saved),
        *_scenario_template_diagnostics(test_data, set(fixtures), scenario_sub_names),
        *_reserved_name_diagnostics(vars_defined | vars_saved | set(fixtures)),
        *_dataflow_diagnostics(scenario, test_data),
        *_verify_diagnostics(scenario),
        *_inline_schema_diagnostics(scenario),
        *_marker_diagnostics(scenario),
        *_parametrize_timing_diagnostics(scenario),
    ]

    scenario_info = ScenarioInfo(
        num_stages=len(scenario.stages),
        stage_names=[stage.name for stage in scenario.stages],
        vars_referenced=sorted(extract_template_variables(test_data)),
        vars_saved=sorted(vars_saved),
        vars_defined=sorted(vars_defined),
        fixtures=fixtures,
    )
    return diagnostics, scenario_info


def _stage_name_diagnostics(scenario: Scenario) -> Iterator[Diagnostic]:
    """HTTPCHAIN001: duplicate stage names."""
    counts = Counter(stage.name for stage in scenario.stages)
    duplicates = {name for name, count in counts.items() if count > 1}
    if duplicates:
        yield diag(DiagnosticCode.DUPLICATE_STAGE, "error", f"Duplicate stage names found: {sorted(duplicates)}", location="stages")


def _fixture_diagnostics(scenario: Scenario, vars_saved: set[str]) -> Iterator[Diagnostic]:
    """HTTPCHAIN002/009: fixtures colliding with same-named variables, and
    scenario-level fixtures shadowing same-named saves.

    002 is scoped per stage, so a fixture and a variable that never coexist in
    one stage are not flagged.
    """
    var_conflicts: set[str] = set()
    for scope in stage_scopes(scenario):
        fixtures_in_stage = scope.scenario_fixtures | scope.stage_fixtures
        vars_in_stage = scope.scenario_substitutions | scope.stage_substitutions | scope.parametrize_params | scope.foreach_params
        var_conflicts |= fixtures_in_stage & vars_in_stage
    if var_conflicts:
        yield diag(DiagnosticCode.FIXTURE_CONFLICT, "error", f"Conflicting fixtures and vars with same names: {sorted(var_conflicts)}")

    shadowed_saves = set(scenario.fixtures) & vars_saved
    if shadowed_saves:
        yield diag(
            DiagnosticCode.FIXTURE_SHADOWS_SAVE,
            "warning",
            f"Saved variables shadowed by scenario-level fixtures: {sorted(shadowed_saves)} (fixture values win in every stage; these saves can never be read)",
        )


def _scenario_template_diagnostics(test_data: dict[str, Any], fixtures: set[str], scenario_sub_names: set[str]) -> Iterator[Diagnostic]:
    """HTTPCHAIN016/017: scenario-level templates resolve against only the
    scenario substitutions, so a fixture (016) or undefined (017) reference
    there is a guaranteed crash at scenario initialization."""
    for key in SCENARIO_TEMPLATE_FIELDS:
        scenario_level_refs = extract_template_variables(test_data.get(key))
        fixture_refs = scenario_level_refs & fixtures
        if fixture_refs:
            yield diag(
                DiagnosticCode.FIXTURE_IN_SCENARIO_TEMPLATE,
                "error",
                f"Fixtures referenced in scenario-level '{key}' templates: {sorted(fixture_refs)} (the scenario-level context never includes fixture values)",
                location=key,
            )
        undefined_refs = scenario_level_refs - fixtures - scenario_sub_names
        if undefined_refs:
            yield diag(
                DiagnosticCode.SCENARIO_UNDEFINED_VAR,
                "error",
                f"Undefined variable(s) in scenario-level '{key}' templates: {sorted(undefined_refs)} (resolved against only scenario substitutions, before any stage runs)",
                location=key,
            )


def _reserved_name_diagnostics(user_names: set[str]) -> Iterator[Diagnostic]:
    """HTTPCHAIN027 (static half): names shadowed by the reserved ``response``
    namespace inside response steps. The carrier covers dynamically-produced
    save keys this cannot see."""
    reserved_conflicts = user_names & {RESPONSE_META_NAME}
    if reserved_conflicts:
        yield diag(
            DiagnosticCode.RESERVED_NAME,
            "warning",
            f"Name(s) {sorted(reserved_conflicts)} are shadowed by the reserved response metadata namespace inside response steps "
            f"(save/verify templates see the HTTP response there, not your value)",
        )


def _dataflow_diagnostics(scenario: Scenario, test_data: dict[str, Any]) -> Iterator[Diagnostic]:
    """HTTPCHAIN003/004: order-aware data-flow analysis.

    Checks every template reference against the phase scopes of
    ``scoping.stage_scopes``. An unavailable reference is a FORWARD_REF when the
    name is saved later (or defined by a later substitution step), else an
    UNDEFINED_VAR. Intra-response step ordering is approximated: a stage's own
    saves count as available to its whole response.
    """
    scopes = stage_scopes(scenario)
    all_saved = extract_saved_variables(scenario)
    first_save_stage: dict[str, int] = {}
    for i, scope in enumerate(scopes):
        for name in scope.saves:
            first_save_stage.setdefault(name, i)

    # raws[i] pairs with scenario.stages[i]: both come from the same
    # order-preserving normalization (models._normalize_stages_input).
    raws = raw_stages(test_data)

    for i, stage in enumerate(scenario.stages):
        scope = scopes[i]
        raw = raws[i] if i < len(raws) and isinstance(raws[i], dict) else {}

        for name in sorted(extract_template_variables(raw.get("parametrize"))):
            if name in scope.scenario_substitutions:
                continue
            yield diag(
                DiagnosticCode.UNDEFINED_VAR,
                "warning",
                f"Stage '{stage.name}': parametrize value references '{name}' — only scenario-level substitutions are in scope when values are resolved",
                location=stage.name,
            )

        for name in sorted(extract_template_variables(raw.get("always_run"))):
            if name in scope.always_run:
                continue
            if name in all_saved:
                j = first_save_stage[name]
                if j == i:
                    msg = f"Stage '{stage.name}': always_run references '{name}', which is only saved in this stage's response — always_run is evaluated before the stage runs"
                else:
                    msg = f"Stage '{stage.name}': always_run references '{name}' before it is saved (saved in stage '{scenario.stages[j].name}')"
                yield diag(DiagnosticCode.FORWARD_REF, "warning", msg, location=stage.name)
            else:
                yield diag(
                    DiagnosticCode.UNDEFINED_VAR,
                    "warning",
                    f"Stage '{stage.name}': always_run references '{name}' — potentially not in scope; only fixtures, "
                    f"parametrize parameters, scenario substitutions, and variables saved by earlier stages are available",
                    location=stage.name,
                )

        # (references, names available to them, is-a-pre-response-phase). Each
        # substitution step is checked against its own scope, not the whole
        # stage's: checking cumulatively is what catches intra-list forward
        # references. A name referenced by several steps is reported once.
        phase_checks: list[tuple[list[str], frozenset[str], bool]] = []
        seen_sub_refs: set[str] = set()
        for entry_refs, prior_sub_names in substitution_step_refs(raw.get("substitutions")):
            entry_refs -= seen_sub_refs
            seen_sub_refs |= entry_refs
            phase_checks.append((sorted(entry_refs), scope.always_run | prior_sub_names, True))
        phase_checks += [
            (sorted(extract_template_variables(raw.get("parallel"))), scope.pre_iteration, True),
            (sorted(extract_template_variables(raw.get("request"))), scope.request, True),
            (sorted(extract_template_variables(raw.get("response"))), scope.response, False),
        ]

        undefined_here: set[str] = set()
        for refs, available, in_request in phase_checks:
            for name in refs:
                if name in available:
                    continue
                if name in scope.stage_substitutions:
                    yield diag(
                        DiagnosticCode.FORWARD_REF,
                        "warning",
                        f"Stage '{stage.name}': substitution references '{name}' before the substitution step that defines it — steps resolve in order",
                        location=stage.name,
                    )
                elif name in all_saved:
                    j = first_save_stage[name]
                    if j == i and in_request:
                        msg = f"Stage '{stage.name}': variable '{name}' is referenced in the request but only saved in this stage's response"
                    else:
                        msg = f"Stage '{stage.name}': variable '{name}' is referenced before it is saved (saved in stage '{scenario.stages[j].name}')"
                    yield diag(DiagnosticCode.FORWARD_REF, "warning", msg, location=stage.name)
                else:
                    undefined_here.add(name)

        if undefined_here:
            yield diag(
                DiagnosticCode.UNDEFINED_VAR,
                "warning",
                f"Stage '{stage.name}': potentially undefined variable(s) referenced: {sorted(undefined_here)}",
                location=stage.name,
            )


def _verify_diagnostics(scenario: Scenario) -> Iterator[Diagnostic]:
    """HTTPCHAIN005/006/007/008/018: stages without verification, verify steps
    that assert nothing, non-template expressions, and contradictory body or
    header declarations."""
    for i, stage in enumerate(scenario.stages):
        if not any(isinstance(step, VerifyStep) for step in stage.response):
            yield diag(DiagnosticCode.NO_VERIFY, "warning", f"Stage '{stage.name}' has no response validation (no verify step)", location=stage.name)

        for k, step in enumerate(stage.response):
            if not isinstance(step, VerifyStep):
                continue
            verify = step.verify
            location = f"stages[{i}].response[{k}].verify"

            if _is_noop_verify(verify):
                yield diag(
                    DiagnosticCode.NOOP_VERIFY,
                    "warning",
                    f"Stage '{stage.name}': verify step asserts nothing (no status, headers, expressions, user functions, or body checks)",
                    location=location,
                )

            # A non-template expression is a non-empty string, hence always
            # truthy at runtime: the assertion silently passes.
            for expr in verify.expressions:
                if isinstance(expr, str) and not is_complete_template(expr):
                    yield diag(
                        DiagnosticCode.NONTEMPLATE_EXPRESSION,
                        "warning",
                        f"Stage '{stage.name}': verify expression {expr!r} is not a template ({{{{ }}}}); it is always truthy and asserts nothing",
                        location=location,
                    )

            yield from _contradiction_diagnostics(
                stage.name,
                "body verification",
                f"{location}.body",
                contains=verify.body.contains,
                not_contains=verify.body.not_contains,
                matches=verify.body.matches,
                not_matches=verify.body.not_matches,
            )

            for header_name, expected in verify.headers.items():
                if not isinstance(expected, HeaderMatcher):
                    continue
                yield from _contradiction_diagnostics(
                    stage.name,
                    f"header '{header_name}' verification",
                    f"{location}.headers.{header_name}",
                    contains=optional_as_list(expected.contains),
                    not_contains=optional_as_list(expected.not_contains),
                    matches=optional_as_list(expected.matches),
                    not_matches=optional_as_list(expected.not_matches),
                )


def _is_noop_verify(verify: Verify) -> bool:
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


def _contradiction_diagnostics(
    stage_name: str,
    what: str,
    location: str,
    *,
    contains: list[Any],
    not_contains: list[Any],
    matches: list[Any],
    not_matches: list[Any],
) -> Iterator[Diagnostic]:
    """HTTPCHAIN007/008, shared by body verification and header matchers.

    Overlap is compared on the raw (unrendered) strings; a contradiction that
    only emerges after rendering is not pursued, since rendering with a partial
    static context risks false positives.
    """
    for required, forbidden, code, noun in (
        (contains, not_contains, DiagnosticCode.CONTAINS_CONTRADICTION, "substring(s)"),
        (matches, not_matches, DiagnosticCode.MATCHES_CONTRADICTION, "pattern(s)"),
    ):
        overlap = {str(value) for value in required} & {str(value) for value in forbidden}
        if overlap:
            yield diag(code, "error", f"Stage '{stage_name}': {what} both requires and forbids {noun}: {sorted(overlap)}", location=location)


def _inline_schema_diagnostics(scenario: Scenario) -> Iterator[Diagnostic]:
    """HTTPCHAIN028: scenario reference directives inside an inline JSON Schema,
    which the resolver treats as opaque and therefore never processes."""

    def directive_keys(node: Any) -> set[str]:
        # String values only: a schema whose `properties` legitimately declares
        # an "$include" property maps it to a schema object. "$ref" is schema
        # vocabulary unless it names a file, which nothing can resolve at runtime.
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
                return set().union(*(directive_keys(item) for item in node))
            case _:
                return set()

    for stage in scenario.stages:
        for step in stage.response:
            if isinstance(step, VerifyStep) and isinstance(step.verify.body.schema, dict):
                found = directive_keys(step.verify.body.schema)
                if found:
                    yield diag(
                        DiagnosticCode.SCHEMA_SCENARIO_DIRECTIVE,
                        "warning",
                        f"Inline JSON schema contains scenario reference directive(s) {sorted(found)}. "
                        f"Inline schemas are standard JSON Schema: scenario directives are not resolved there. "
                        f"Inline the shared content, or reference the schema by file path instead.",
                        location=stage.name,
                    )


def _marker_diagnostics(scenario: Scenario) -> Iterator[Diagnostic]:
    """HTTPCHAIN019: marker expressions, parsed here with the same parser
    collection uses, so ``validate`` stays a faithful pre-flight check."""

    def check(marks: list[str], location: str) -> Iterator[Diagnostic]:
        for mark in marks:
            try:
                # Constructing an unregistered mark emits PytestUnknownMarkWarning,
                # which is noise here — only parseability matters.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    make_marker(mark)
            except (ValueError, SyntaxError) as e:
                yield diag(DiagnosticCode.INVALID_MARKER, "error", f"Invalid marker {mark!r}: {e}", location=location)

    yield from check(scenario.marks, "marks")
    for i, stage in enumerate(scenario.stages):
        yield from check(stage.marks, f"stages[{i}].marks")


def _parametrize_timing_diagnostics(scenario: Scenario) -> Iterator[Diagnostic]:
    """HTTPCHAIN025 (info): template parametrize values force scenario
    substitutions to resolve at collection time. Shares its predicate with the
    factory, so validator and runtime agree by construction."""
    for i, stage in enumerate(scenario.stages):
        if parametrize_values_contain_template(stage.parametrize):
            yield diag(
                DiagnosticCode.PARAMETRIZE_COLLECTION_RESOLUTION,
                "info",
                f"Stage '{stage.name}' has template parametrize values: scenario-level substitutions for this scenario "
                f"resolve at collection time (pytest needs concrete parameter values), including any user functions they call",
                location=f"stages[{i}].parametrize",
            )
