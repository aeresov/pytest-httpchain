"""Semantic checks a JSON Schema cannot express, run by both the CLI and pytest collection.

Each check family is a generator of `Diagnostic`; `check_scenario` composes them.
"""

import json
import re
import warnings
from collections import Counter
from collections.abc import Generator, Iterator, Sequence
from typing import Any

import pytest

from pytest_httpchain.jsonref import json_equal
from pytest_httpchain.models import (
    FunctionsSubstitution,
    HeaderMatcher,
    JMESPathMatcher,
    SaveStep,
    Scenario,
    Stage,
    Substitution,
    SubstitutionsSave,
    UserFunctionKwargs,
    Verify,
    VerifyStep,
    is_relative_url,
    parametrize_values_contain_template,
)
from pytest_httpchain.scoping import (
    RESPONSE_META_NAME,
    SCENARIO_TEMPLATE_FIELDS,
    DefinedNames,
    defined_names,
    extract_builtin_stand_ins,
    extract_defined_variables,
    extract_invalid_expressions,
    extract_saved_variables,
    extract_template_variables,
    extract_uncalled_builtins,
    raw_stages,
    response_step_templates,
    saved_in_stage,
    stage_scopes,
    substitution_names,
    substitution_step_templates,
)
from pytest_httpchain.templates import CALL_ONLY_BUILTINS, TEMPLATE_BUILTINS, TEMPLATE_PATTERN, call_form, contains_template, is_complete_template
from pytest_httpchain.utils import make_marker, optional_as_list, path_segment, xdist_group_names
from pytest_httpchain.validation.diagnostics import Diagnostic, DiagnosticCode, ScenarioInfo, diag
from pytest_httpchain.validation.loader import is_jmespath_expectations_position


def _scenario_fixtures(scenario: Scenario) -> list[str]:
    """Every fixture name the scenario or any stage requests."""
    return sorted({*scenario.fixtures, *(name for stage in scenario.stages for name in stage.fixtures)})


def check_scenario(scenario: Scenario, test_data: dict[str, Any]) -> list[Diagnostic]:
    """Semantic checks on an already-loaded, schema-valid scenario.

    Takes the parsed ``test_data`` alongside the validated model so template
    references can be read from the raw text. The composition order below is the
    reported order.
    """
    fixtures = _scenario_fixtures(scenario)
    vars_defined = extract_defined_variables(scenario)
    vars_saved = extract_saved_variables(scenario)
    scenario_sub_names = substitution_names(scenario.substitutions)
    defined = defined_names(scenario)

    return [
        *_stage_name_diagnostics(scenario),
        *_fixture_diagnostics(scenario, vars_saved),
        *_invalid_expression_diagnostics(scenario, test_data),
        *_scenario_template_diagnostics(scenario, test_data, set(fixtures), scenario_sub_names, defined),
        *_reserved_name_diagnostics(vars_defined | vars_saved | set(fixtures)),
        *_dataflow_diagnostics(scenario, test_data, defined),
        *_uncalled_builtin_diagnostics(scenario, test_data, defined),
        *_relative_url_diagnostics(scenario),
        *_verify_diagnostics(scenario),
        *_inline_schema_diagnostics(scenario),
        *_marker_diagnostics(scenario),
        *_parametrize_timing_diagnostics(scenario),
        *_template_key_diagnostics(test_data),
        *_template_kwargs_diagnostics(scenario),
    ]


def describe_scenario(scenario: Scenario, test_data: dict[str, Any]) -> ScenarioInfo:
    """Structural summary for the CLI's ``--format json`` payload.

    Separate from `check_scenario` because only the CLI wants it: pytest
    collection needs the diagnostics alone, and used to discard this as ``_``.
    """
    defined = defined_names(scenario)
    return ScenarioInfo(
        num_stages=len(scenario.stages),
        stage_names=[stage.name for stage in scenario.stages],
        vars_referenced=sorted(extract_template_variables(test_data, defined=defined) | extract_builtin_stand_ins(test_data, defined=defined)),
        vars_saved=sorted(extract_saved_variables(scenario)),
        vars_defined=sorted(extract_defined_variables(scenario)),
        fixtures=_scenario_fixtures(scenario),
    )


def _stage_name_diagnostics(scenario: Scenario) -> Iterator[Diagnostic]:
    """HTTPCHAIN001/032: duplicate stage names, and a ``::`` in one.

    Unnamed stages are excluded: ``Stage.name`` is optional and defaults to ``""``,
    so counting the default made two stages that simply omit it collide — a hard
    rejection of schema-valid input, naming a field the author never wrote.

    The name becomes the stage's test name, so a ``::`` in it lands in the node
    id, where it is pytest's separator. Rejected rather than rewritten: the
    report, ``-k`` and ``validate`` then all show the name as written.
    """
    counts = Counter(stage.name for stage in scenario.stages if stage.name)
    duplicates = {name for name, count in counts.items() if count > 1}
    if duplicates:
        yield diag(DiagnosticCode.DUPLICATE_STAGE, f"Duplicate stage names found: {sorted(duplicates)}", location="stages")

    for i, stage in enumerate(scenario.stages):
        if "::" in stage.name:
            yield diag(
                DiagnosticCode.NODE_ID_SEPARATOR_IN_STAGE_NAME,
                f"Stage name {stage.name!r} contains '::', which separates the parts of a pytest node id: the stage's test cannot be run "
                f"by its node id, and pytest-xdist --dist loadscope would run it apart from the rest of the scenario, without the earlier "
                f"stages' saved values. Rename the stage without '::'.",
                location=f"stages[{i}].name",
            )


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
        yield diag(DiagnosticCode.FIXTURE_CONFLICT, f"Conflicting fixtures and vars with same names: {sorted(var_conflicts)}")

    shadowed_saves = set(scenario.fixtures) & vars_saved
    if shadowed_saves:
        yield diag(
            DiagnosticCode.FIXTURE_SHADOWS_SAVE,
            f"Saved variables shadowed by scenario-level fixtures: {sorted(shadowed_saves)} (fixture values win in every stage; these saves can never be read)",
        )


def _builtin_fallback(names: set[str], *, fails: str = "fails the stage") -> str:
    """What an out-of-scope read of a built-in's name gets instead: the
    built-in function. Appended to the reference messages, where "undefined"
    alone would be a mystery for a name the template can evaluate.

    Most built-ins (``max``) are then used as the value. A call-only one
    (`CALL_ONLY_BUILTINS`: ``now``, ``env``, ...) is no value, and the runtime
    refuses a template that renders to it, so for those the note says what that
    refusal ``fails``: the stage, or where the template resolves outside one,
    collection or scenario initialization.
    """
    notes: list[str] = []
    if used := sorted(names & (TEMPLATE_BUILTINS - CALL_ONLY_BUILTINS)):
        if len(used) == 1:
            notes.append(f"where no definition of '{used[0]}' is in scope, the template built-in of that name is used instead")
        else:
            notes.append(f"where no definition of {used} is in scope, the template built-ins of those names are used instead")
    if refused := sorted(names & CALL_ONLY_BUILTINS):
        if len(refused) == 1:
            notes.append(f"where no definition of '{refused[0]}' is in scope, the name is the template built-in function, not a value, and a template that renders to it {fails}")
        else:
            notes.append(f"where no definition of {refused} is in scope, the names are the template built-in functions, not values, and a template that renders to one {fails}")
    return "".join(f"; {note}" for note in notes)


# What the runtime's refusal of a template that renders to a call-only
# built-in fails, where a template resolves outside any stage
# (`_builtin_fallback`, HTTPCHAIN035).
_FAILS_SCENARIO_INIT = "crashes scenario initialization"
_FAILS_COLLECTION = "fails the scenario's collection"
_FAILS_STAGE = "fails the stage"


def _scenario_level_fails(scenario: Scenario, key: str) -> str:
    """What a template that fails in the scenario-level field ``key`` fails.

    The ``substitutions`` of a scenario with a stage whose parametrize values
    are templates resolve at collection (HTTPCHAIN025, on the factory's own
    predicate), so a failure there fails the scenario's collection. Otherwise
    they, and ``auth``, ``ssl`` and ``client`` always, resolve at scenario
    initialization."""
    if key == "substitutions" and any(parametrize_values_contain_template(stage.parametrize) for stage in scenario.stages):
        return _FAILS_COLLECTION
    return _FAILS_SCENARIO_INIT


def _template_refs(templates: Any, defined: DefinedNames) -> tuple[set[str], set[str]]:
    """``(references, stand-ins)`` of some template text: the names whose
    absence the undefined-name codes report, and the built-ins' names used only
    as functions, which out of scope get the built-in in their place (036)."""
    stand_ins = extract_builtin_stand_ins(templates, defined=defined)
    return extract_template_variables(templates, defined=defined) - stand_ins, stand_ins


def _stand_in_diagnostic(where: str, names: set[str], location: str, *, scope_note: str = "") -> Diagnostic:
    """HTTPCHAIN036: built-ins' names the scenario defines too, used only as
    functions (`scoping.extract_builtin_stand_ins`) where the scenario's own
    definition is not in scope.

    A warning, whatever the phase: the built-in runs in its place, which never
    fails, so it is reported as the built-in standing in, never through the
    undefined-name codes, whose scenario-level 016/017 are errors.
    """
    listed = sorted(names)
    calls = ", ".join(f"{name}()" for name in listed)
    if len(listed) == 1:
        own = f"the scenario's own definition of '{listed[0]}' is not in scope there{scope_note}, so the template built-in runs instead"
    else:
        own = f"the scenario's own definitions of {listed} are not in scope there{scope_note}, so the template built-ins run instead"
    uses = f"the function {calls}" if len(listed) == 1 else f"the functions {calls}"
    return diag(DiagnosticCode.BUILTIN_STANDS_IN, f"{where} uses {uses}, but {own}", location=location)


def _scenario_template_diagnostics(scenario: Scenario, test_data: dict[str, Any], fixtures: set[str], scenario_sub_names: set[str], defined: DefinedNames) -> Iterator[Diagnostic]:
    """HTTPCHAIN016/017/036: scenario-level templates resolve against only the
    scenario substitutions, so a fixture (016) or undefined (017) reference
    there is a crash at scenario initialization (at collection, for the
    ``substitutions`` of a scenario that resolves them there,
    `_scenario_level_fails`), or for a read of a built-in's name the built-in
    function in the value.

    The ``substitutions`` list itself resolves strictly in order (the runtime
    computes each step's context before that step's names land), so its entries
    are checked against only PRIOR steps' names — a forward or same-step
    reference crashes exactly like an undefined one. ``auth``, ``ssl`` and
    ``client`` resolve after the whole list and see every name.

    A built-in's name the scenario defines too, called or passed as a function
    (036), is the exception: out of that definition's scope the built-in runs
    in its place, which is no crash, so it is a warning.
    """
    for key in SCENARIO_TEMPLATE_FIELDS:
        if key == "substitutions":
            templates_and_scopes = list(substitution_step_templates(test_data.get(key)))
        else:
            templates_and_scopes = [(test_data.get(key), frozenset(scenario_sub_names))]
        fails = _scenario_level_fails(scenario, key)

        fixture_refs: set[str] = set()
        forward_refs: set[str] = set()
        undefined_refs: set[str] = set()
        stand_ins: set[str] = set()
        for templates, in_scope in templates_and_scopes:
            entry_refs, entry_stand_ins = _template_refs(templates, defined)
            fixture_refs |= entry_refs & fixtures
            unavailable = entry_refs - fixtures - in_scope
            forward_refs |= unavailable & scenario_sub_names
            undefined_refs |= unavailable - scenario_sub_names
            stand_ins |= entry_stand_ins - in_scope

        if fixture_refs:
            yield diag(
                DiagnosticCode.FIXTURE_IN_SCENARIO_TEMPLATE,
                f"Fixtures referenced in scenario-level '{key}' templates: {sorted(fixture_refs)} (the scenario-level context never includes fixture values"
                f"{_builtin_fallback(fixture_refs, fails=fails)})",
                location=key,
            )
        if forward_refs:
            # A built-in's name crashes only where a template renders to it,
            # which is what the fallback note says for it.
            crash = ""
            if forward_refs - TEMPLATE_BUILTINS:
                crash = "; this crashes at scenario initialization" if fails == _FAILS_SCENARIO_INIT else f"; this {fails}"
            yield diag(
                DiagnosticCode.SCENARIO_UNDEFINED_VAR,
                f"Scenario-level '{key}' references name(s) before the substitution step that defines them: {sorted(forward_refs)} "
                f"(steps resolve strictly in order{crash}{_builtin_fallback(forward_refs, fails=fails)})",
                location=key,
            )
        if undefined_refs:
            yield diag(
                DiagnosticCode.SCENARIO_UNDEFINED_VAR,
                f"Undefined variable(s) in scenario-level '{key}' templates: {sorted(undefined_refs)} (resolved against only scenario substitutions, before any stage runs"
                f"{_builtin_fallback(undefined_refs, fails=fails)})",
                location=key,
            )
        if stand_ins:
            seen = "a scenario-level substitution step sees only the steps before it" if key == "substitutions" else "scenario-level templates see only scenario substitutions"
            note = f" ({seen}: no fixture, and nothing a stage defines)"
            yield _stand_in_diagnostic(f"Scenario-level '{key}'", stand_ins, key, scope_note=note)


def _reserved_name_diagnostics(user_names: set[str]) -> Iterator[Diagnostic]:
    """HTTPCHAIN027 (static half): names shadowed by the reserved ``response``
    namespace inside response steps. The carrier covers dynamically-produced
    save keys this cannot see."""
    reserved_conflicts = user_names & {RESPONSE_META_NAME}
    if reserved_conflicts:
        yield diag(
            DiagnosticCode.RESERVED_NAME,
            f"Name(s) {sorted(reserved_conflicts)} are shadowed by the reserved response metadata namespace inside response steps "
            f"(save/verify templates see the HTTP response there, not your value)",
        )


def _parametrize_rendered_values(raw_parametrize: Any) -> Any:
    """The parametrize subtree minus each step's ``ids``, which pytest uses
    verbatim for display and never renders (the same carve-out
    ``models.parametrize_values_contain_template`` encodes)."""
    if isinstance(raw_parametrize, list):
        return [{k: v for k, v in entry.items() if k != "ids"} if isinstance(entry, dict) else entry for entry in raw_parametrize]
    return raw_parametrize


def _skippable_save_diagnostic(scenario: Scenario, i: int, where: str, name: str, location: str) -> Diagnostic:
    """HTTPCHAIN003 for a reference to ``name`` that only earlier stages with a
    ``skip_if`` save (`scoping.StageScopes.skippable_saves`): a skip leaves the
    chain healthy, so stage ``i`` runs, and finds the name undefined, whenever
    those stages skipped. ``where`` is the phase that reads it."""
    savers = [f"'{stage.name}'" for stage in scenario.stages[:i] if name in saved_in_stage(stage)]
    if len(savers) == 1:
        saved = f"which only stage {savers[0]} saves, and it has skip_if: when it skips"
    else:
        saved = f"which only stages {', '.join(savers)} save, and each has skip_if: when they all skip"
    return diag(
        DiagnosticCode.UNDEFINED_VAR,
        f"Stage '{scenario.stages[i].name}': {where} references '{name}', {saved}, '{name}' is undefined here — read it with get('{name}', <default>){_builtin_fallback({name})}",
        location=location,
    )


def _dataflow_diagnostics(scenario: Scenario, test_data: dict[str, Any], defined: DefinedNames) -> Iterator[Diagnostic]:
    """HTTPCHAIN003/004/036: order-aware data-flow analysis.

    Checks every template reference against the phase scopes of
    ``scoping.stage_scopes``. An unavailable reference is a FORWARD_REF when the
    name is saved later (by a later stage, or by a later step of this stage's
    own response) or defined by a later substitution step, else an UNDEFINED_VAR.
    An available one is an UNDEFINED_VAR too where only earlier stages with a
    ``skip_if`` save it, and nothing else of that name is in scope: a skipped
    stage, unlike a failed one, does not stop the stages after it. A built-in's
    name the scenario defines too, called or passed as a function where that
    definition is not in scope, gets the built-in instead: a BUILTIN_STANDS_IN.
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
        # The same phases, as they are when every earlier stage with a skip_if skipped.
        skipped = scope.when_skipped
        raw = raws[i] if i < len(raws) and isinstance(raws[i], dict) else {}

        parametrize_refs, parametrize_stand_ins = _template_refs(_parametrize_rendered_values(raw.get("parametrize")), defined)
        for name in sorted(parametrize_refs):
            if name in scope.scenario_substitutions:
                continue
            yield diag(
                DiagnosticCode.UNDEFINED_VAR,
                f"Stage '{stage.name}': parametrize value references '{name}' — only scenario-level substitutions are in scope when values are resolved"
                f"{_builtin_fallback({name}, fails=_FAILS_COLLECTION)}",
                location=f"stages[{i}].parametrize",
            )
        if parametrize_stand_ins := parametrize_stand_ins - scope.scenario_substitutions:
            yield _stand_in_diagnostic(
                f"Stage '{stage.name}': parametrize value",
                parametrize_stand_ins,
                f"stages[{i}].parametrize",
                scope_note=" (values resolve against scenario-level substitutions only)",
            )

        always_run_refs, always_run_stand_ins = _template_refs(raw.get("always_run"), defined)
        for name in sorted(always_run_refs):
            if name in scope.always_run:
                if name not in skipped.always_run:
                    yield _skippable_save_diagnostic(scenario, i, "always_run", name, f"stages[{i}].always_run")
                continue
            if name in all_saved:
                j = first_save_stage[name]
                if j == i:
                    msg = f"Stage '{stage.name}': always_run references '{name}', which is only saved in this stage's response — always_run is evaluated before the stage runs"
                else:
                    msg = f"Stage '{stage.name}': always_run references '{name}' before it is saved (saved in stage '{scenario.stages[j].name}')"
                yield diag(DiagnosticCode.FORWARD_REF, msg + _builtin_fallback({name}), location=f"stages[{i}].always_run")
            else:
                yield diag(
                    DiagnosticCode.UNDEFINED_VAR,
                    f"Stage '{stage.name}': always_run references '{name}' — potentially not in scope; only fixtures, "
                    f"parametrize parameters, scenario substitutions, and variables saved by earlier stages are available{_builtin_fallback({name})}",
                    location=f"stages[{i}].always_run",
                )
        if always_run_stand_ins := always_run_stand_ins - scope.always_run:
            yield _stand_in_diagnostic(f"Stage '{stage.name}': always_run", always_run_stand_ins, f"stages[{i}].always_run")

        # (phase, template text, names available to it, and those of them
        # still available when every earlier stage with a skip_if skipped).
        # Each substitution and response step is checked against its own
        # scope, not the whole stage's: checking cumulatively is what catches
        # intra-list forward references. A name referenced by several steps is
        # reported once. The phase is carried rather than a bare "is this
        # pre-response?" flag because it is also what the author needs told:
        # "undefined in this stage" sends them hunting, "undefined in this
        # stage's request" does not.
        phase_checks: list[tuple[str, Any, frozenset[str], frozenset[str]]] = [
            ("substitutions", templates, scope.always_run | prior_sub_names, skipped.always_run | prior_sub_names)
            for templates, prior_sub_names in substitution_step_templates(raw.get("substitutions"))
        ]
        phase_checks += [
            ("skip_if", raw.get("skip_if"), scope.pre_iteration, skipped.pre_iteration),
            ("parallel", raw.get("parallel"), scope.pre_iteration, skipped.pre_iteration),
            ("retry", raw.get("retry"), scope.pre_iteration, skipped.pre_iteration),
            ("request", raw.get("request"), scope.request, skipped.request),
        ]
        phase_checks += [
            ("response", templates, scope.response | prior_saves, skipped.response | prior_saves) for templates, prior_saves in response_step_templates(stage, raw.get("response"))
        ]

        # Insertion order, so output stays deterministic across runs.
        undefined_by_phase: dict[str, set[str]] = {}
        stand_ins_by_phase: dict[str, set[str]] = {}
        seen_refs: dict[str, set[str]] = {}
        for phase, templates, available, available_when_skipped in phase_checks:
            refs, stand_ins = _template_refs(templates, defined)
            refs -= seen_refs.setdefault(phase, set())
            seen_refs[phase] |= refs
            for name in sorted(refs):
                if name in available:
                    if name not in available_when_skipped:
                        yield _skippable_save_diagnostic(scenario, i, phase, name, f"stages[{i}].{phase}")
                    continue
                if name in scope.stage_substitutions:
                    yield diag(
                        DiagnosticCode.FORWARD_REF,
                        f"Stage '{stage.name}': substitution references '{name}' before the substitution step that defines it — steps resolve in order{_builtin_fallback({name})}",
                        location=f"stages[{i}].substitutions",
                    )
                elif name in all_saved:
                    j = first_save_stage[name]
                    if j != i:
                        msg = f"Stage '{stage.name}': variable '{name}' is referenced before it is saved (saved in stage '{scenario.stages[j].name}')"
                    elif phase == "response":
                        msg = f"Stage '{stage.name}': response step references '{name}' before the save that produces it — steps resolve in order"
                    else:
                        msg = f"Stage '{stage.name}': {phase} references '{name}', which is only saved in this stage's response"
                    yield diag(DiagnosticCode.FORWARD_REF, msg + _builtin_fallback({name}), location=f"stages[{i}].{phase}")
                else:
                    undefined_by_phase.setdefault(phase, set()).add(name)
            if stand_ins := stand_ins - available:
                stand_ins_by_phase.setdefault(phase, set()).update(stand_ins)

        for phase, names in undefined_by_phase.items():
            yield diag(
                DiagnosticCode.UNDEFINED_VAR,
                f"Stage '{stage.name}': {phase} references potentially undefined variable(s): {sorted(names)}{_builtin_fallback(names)}",
                location=f"stages[{i}].{phase}",
            )
        for phase, names in stand_ins_by_phase.items():
            yield _stand_in_diagnostic(f"Stage '{stage.name}': {phase}", names, f"stages[{i}].{phase}")


# The raw stage fields a template renders in, in resolution order.
_STAGE_TEMPLATE_FIELDS = ("parametrize", "always_run", "substitutions", "skip_if", "parallel", "retry", "request", "response")


def _rendered_stage_field(stage: Stage, field: str, raw_value: Any) -> Any:
    """The part of a raw stage field the runtime renders: the reference checks'
    view of it (parametrize values without ``ids``, substitution steps without
    function kwargs, a substitutions-save's entries alone)."""
    match field:
        case "parametrize":
            return _parametrize_rendered_values(raw_value)
        case "substitutions":
            return [templates for templates, _ in substitution_step_templates(raw_value)]
        case "response":
            return [templates for templates, _ in response_step_templates(stage, raw_value)]
        case _:
            return raw_value


def _rendered_template_fields(scenario: Scenario, test_data: dict[str, Any]) -> Iterator[tuple[Any, str, str, str]]:
    """``(text, where, location, fails)`` for each field a template renders in,
    scenario-level and per stage: the part of it the runtime renders, as the
    reference checks read it (`_rendered_stage_field`: a function's kwargs and
    a substitutions-save's ``description`` are never rendered, so a template
    there is dead text), how a message names the field, its location, and what
    a template that fails there fails (collection for a parametrize value,
    scenario initialization or collection at scenario level,
    `_scenario_level_fails`)."""
    for key in SCENARIO_TEMPLATE_FIELDS:
        subtree = test_data.get(key)
        if key == "substitutions":
            subtree = [templates for templates, _ in substitution_step_templates(subtree)]
        yield subtree, f"Scenario-level '{key}'", key, _scenario_level_fails(scenario, key)
    raws = raw_stages(test_data)
    for i, stage in enumerate(scenario.stages):
        raw = raws[i] if i < len(raws) and isinstance(raws[i], dict) else {}
        for field in _STAGE_TEMPLATE_FIELDS:
            fails = _FAILS_COLLECTION if field == "parametrize" else _FAILS_STAGE
            yield _rendered_stage_field(stage, field, raw.get(field)), f"Stage '{stage.name}': {field}", f"stages[{i}].{field}", fails


def _invalid_expression_diagnostics(scenario: Scenario, test_data: dict[str, Any]) -> Iterator[Diagnostic]:
    """HTTPCHAIN037/038: a template that holds no expression the engine
    evaluates (`templates.parse_expression`): a syntax error (``{{ 1 + }}``,
    or a dict literal cut short by its ``}}}``), ``{{ a; b }}``, an assignment
    (``{{ user.active = True }}``, ``+=``, ``:=``), another statement, or
    anything else the engine refuses from its text alone (a lambda,
    ``doc._id``, ``fns[0]()``).

    The runtime refuses it with the same reason, wherever it renders. Such a
    template names nothing, so this is its one finding, where reading its text
    as a regex's identifiers had the reference checks report ``True`` and
    attribute names as undefined too. Read where the reference checks read
    (`_rendered_template_fields`); each template once per field.

    The severity follows what it fails. Where it fails a stage (037, a
    warning, as an undefined name there is), the stages before it still run.
    Where it fails before any stage runs (038, an error, as 017 is), nothing
    does: at scenario level it fails initialization, and with it every stage,
    and in a stage's parametrize value the collection of the whole scenario,
    which resolves the values. The reason ends the message, as it ends the
    runtime's: Python's own often ends in a ``?``.
    """
    for subtree, where, location, fails in _rendered_template_fields(scenario, test_data):
        code = DiagnosticCode.INVALID_EXPRESSION if fails == _FAILS_STAGE else DiagnosticCode.SCENARIO_INVALID_EXPRESSION
        for template, reason in extract_invalid_expressions(subtree).items():
            yield diag(code, f"{where} has an invalid expression '{template}', and rendering it {fails}: {reason}", location=location)


def _uncalled_builtin_diagnostics(scenario: Scenario, test_data: dict[str, Any], defined: DefinedNames) -> Iterator[Diagnostic]:
    """HTTPCHAIN035: a call-only built-in written without its parentheses
    (``{{ now }}``, ``{{ env }}``).

    A template that renders to the function itself fails its stage at runtime
    (collection for a parametrize value; at scenario level, initialization or
    collection, `_scenario_level_fails`), and inside an expression
    (``str(now)``) the function goes wherever the value would. None of these
    built-ins (`CALL_ONLY_BUILTINS`) is any use as a value, so each such use
    is flagged where it is written; `scoping.extract_uncalled_builtins` leaves
    out a built-in handed to a function that may take one (a ``key=``, a user
    function's argument, a fixture's method's) and a name the scenario defines
    itself, which the reference checks (003/004) cover. Only text the runtime renders is read, as
    the reference checks read it (`_rendered_template_fields`).
    """
    for subtree, where, location, fails in _rendered_template_fields(scenario, test_data):
        names = sorted(extract_uncalled_builtins(subtree, defined=defined))
        if names:
            what = f"the built-in function '{names[0]}' without calling it" if len(names) == 1 else f"the built-in functions {names} without calling them"
            yield diag(
                DiagnosticCode.UNCALLED_BUILTIN,
                f"{where} uses {what}: the template gets the function itself, not its value, and one that renders to a function {fails}. "
                f"Write {', '.join(call_form(name) for name in names)}",
                location=location,
            )


def _relative_url_diagnostics(scenario: Scenario) -> Iterator[Diagnostic]:
    """HTTPCHAIN034: a stage URL relative to a ``client.base_url`` the scenario
    does not set, so the request has nowhere to go.

    A templated URL counts once its literal text before the first template
    makes it relative (``/users/{{ id }}``). One starting with a template
    (``{{ server }}/users``) is known only once rendered: the request builder
    fails that stage instead, with the same message.
    """
    if scenario.client.base_url is not None:
        return
    for i, stage in enumerate(scenario.stages):
        url = stage.request.url
        template = re.search(TEMPLATE_PATTERN, url)
        # The literal text before a template is relative when it already ends
        # the first segment (a `/`, `?` or `#`) without a `:`, which a scheme needs.
        if is_relative_url(url) if template is None else re.match(r"[^:/?#]*[/?#]", url[: template.start()]):
            yield diag(
                DiagnosticCode.RELATIVE_URL_WITHOUT_BASE_URL,
                f"Stage '{stage.name}': request URL {url!r} is relative, but the scenario sets no client.base_url to resolve it against. "
                f"Set client.base_url, or make the URL absolute.",
                location=f"stages[{i}].request.url",
            )


def _verify_diagnostics(scenario: Scenario) -> Iterator[Diagnostic]:
    """HTTPCHAIN005/006/007/008/018: stages without verification, verify steps
    that assert nothing, non-template expressions, and contradictory body,
    header or jmespath matcher declarations."""
    for i, stage in enumerate(scenario.stages):
        if not any(isinstance(step, VerifyStep) for step in stage.response):
            yield diag(DiagnosticCode.NO_VERIFY, f"Stage '{stage.name}' has no response validation (no verify step)", location=f"stages[{i}]")

        for k, step in enumerate(stage.response):
            if not isinstance(step, VerifyStep):
                continue
            verify = step.verify
            location = f"stages[{i}].response[{k}].verify"

            if _is_noop_verify(verify):
                yield diag(
                    DiagnosticCode.NOOP_VERIFY,
                    f"Stage '{stage.name}': verify step asserts nothing (no status, headers, jmespath, expressions, user functions, or body checks)",
                    location=location,
                )

            # A non-template expression is a plain string, never the bool an
            # expression must evaluate to, so it fails the stage at runtime.
            for expr in verify.expressions:
                if isinstance(expr, str) and not is_complete_template(expr):
                    yield diag(
                        DiagnosticCode.NONTEMPLATE_EXPRESSION,
                        f"Stage '{stage.name}': verify expression {expr!r} is not a template ({{{{ }}}}); an expression must evaluate to a bool, so this fails at runtime",
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

            for expression, expected in verify.jmespath.items():
                if isinstance(expected, JMESPathMatcher):
                    yield from _jmespath_contradiction_diagnostics(stage.name, expression, f"{location}.jmespath{path_segment(expression)}", expected)


def _is_noop_verify(verify: Verify) -> bool:
    body = verify.body
    return (
        verify.status is None
        and not verify.headers
        and not verify.jmespath
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
            yield diag(code, f"Stage '{stage_name}': {what} both requires and forbids {noun}: {sorted(overlap)}", location=location)


def _jmespath_contradiction_diagnostics(stage_name: str, expression: str, location: str, matcher: JMESPathMatcher) -> Iterator[Diagnostic]:
    """HTTPCHAIN007/008 for a ``verify.jmespath`` matcher, whose keys hold one
    operand each: contains and not_contains the same JSON value, matches and
    not_matches the same pattern. No value passes both.

    A key counts as set by ``model_fields_set``, since null is an operand of
    contains and not_contains. The contains operands are JSON values, compared
    as the check compares them (`json_equal`: 1 is 1.0, true is not 1); the
    patterns are compared as written. Either may be a template, compared as
    unrendered text, as `_contradiction_diagnostics` does.
    """
    what = f"Stage '{stage_name}': jmespath {expression!r} verification both requires and forbids"
    keys = matcher.model_fields_set
    if {"contains", "not_contains"} <= keys and json_equal(matcher.contains, matcher.not_contains):
        yield diag(DiagnosticCode.CONTAINS_CONTRADICTION, f"{what} {json.dumps(matcher.contains, ensure_ascii=False)} (contains and not_contains)", location=location)
    if {"matches", "not_matches"} <= keys and matcher.matches == matcher.not_matches:
        yield diag(DiagnosticCode.MATCHES_CONTRADICTION, f"{what} pattern {json.dumps(matcher.matches, ensure_ascii=False)} (matches and not_matches)", location=location)


def _inline_schema_diagnostics(scenario: Scenario) -> Iterator[Diagnostic]:
    """HTTPCHAIN028: scenario reference directives inside an inline JSON Schema,
    which the resolver treats as opaque and therefore never processes.

    A ``$ref`` is not one: it is the schema's own, and resolves at runtime,
    to a local file too (`body_schema`), where ``$include`` and ``$merge``
    are no JSON Schema keyword at all.
    """

    def directive_keys(root: Any) -> set[str]:
        # String values only: a schema whose `properties` legitimately declares
        # an "$include" property maps it to a schema object.
        # Iterative: the meta-check never descends into `enum`/`const`/`default`
        # values, so a recursive walk overflowed on one nested a few hundred
        # levels deep.
        found: set[str] = set()
        pending = [root]
        while pending:
            match pending.pop():
                case dict() as node:
                    for key, value in node.items():
                        if key in ("$include", "$merge") and isinstance(value, str):
                            found.add(key)
                        pending.append(value)
                case list() as items:
                    pending.extend(items)
        return found

    for i, stage in enumerate(scenario.stages):
        for k, step in enumerate(stage.response):
            if isinstance(step, VerifyStep) and isinstance(step.verify.body.schema, dict):
                found = directive_keys(step.verify.body.schema)
                if found:
                    yield diag(
                        DiagnosticCode.SCHEMA_SCENARIO_DIRECTIVE,
                        f"Inline JSON schema contains scenario reference directive(s) {sorted(found)}. "
                        f"Inline schemas are standard JSON Schema: scenario directives are not resolved there. "
                        f"Use a JSON Schema $ref instead (a local file resolves relative to the scenario's directory, "
                        f"with a JSON pointer after its '#'), or reference the schema by file path.",
                        location=f"stages[{i}].response[{k}].verify.body.schema",
                    )


def _marker_diagnostics(scenario: Scenario) -> Iterator[Diagnostic]:
    """HTTPCHAIN019/031/033: marker expressions, parsed here with the same
    parser collection uses, so ``validate`` stays a faithful pre-flight check;
    and ``xdist_group`` marks that would make ``--dist loadgroup`` split the chain.

    pytest-xdist joins every group name on a test into one group. Every stage
    carries the scenario's group, declared or automatic, so a stage adding a
    name of its own is in a group apart from its siblings; one repeating a
    declared name changes nothing. xdist also reads the group back from the
    text after the node id's last ``@``, and drops it when a ``]`` follows
    that ``@``: a declared name with a ``]`` after its own last ``@`` leaves
    every stage in a work unit of its own. With several declared names, testing
    each on its own can over-report but never miss: the last ``]`` of the joined
    name falls in a name that fails the test by itself.
    """

    def parse(marks: list[str], location: str) -> Generator[Diagnostic, None, list[tuple[str, pytest.MarkDecorator]]]:
        parsed = []
        for mark in marks:
            try:
                # Constructing an unregistered mark emits PytestUnknownMarkWarning,
                # which is noise here — only parseability matters.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    parsed.append((mark, make_marker(mark)))
            except Exception as e:
                # Broad on purpose: pytest's MarkGenerator raises AttributeError
                # for reserved names ('_foo'), ast.literal_eval TypeError for
                # exotic argument nodes — any parse failure must become a
                # diagnostic, not a validator crash.
                yield diag(DiagnosticCode.INVALID_MARKER, f"Invalid marker {mark!r}: {e}", location=location)
        return parsed

    scenario_marks = yield from parse(scenario.marks, "marks")
    for mark, marker in scenario_marks:
        for group in sorted(xdist_group_names([marker])):
            if group.rfind("]") > group.rfind("@"):
                yield diag(
                    DiagnosticCode.UNREADABLE_XDIST_GROUP,
                    f"Marker {mark!r}: pytest-xdist ignores a group whose name has a ']' after its last '@', so --dist loadgroup "
                    f"would not keep the scenario's stages on one worker, and a stage could run without the earlier stages' saved "
                    f"values. Remove the ']' from {group!r}.",
                    location="marks",
                )
    scenario_groups = xdist_group_names(marker for _, marker in scenario_marks)

    for i, stage in enumerate(scenario.stages):
        location = f"stages[{i}].marks"
        stage_marks = yield from parse(stage.marks, location)
        for mark, marker in stage_marks:
            if not xdist_group_names([marker]) <= scenario_groups:
                yield diag(
                    DiagnosticCode.STAGE_XDIST_GROUP,
                    f"Stage '{stage.name}': marker {mark!r} adds a pytest-xdist group the scenario does not declare, and xdist joins "
                    f"every group on a test into one, so --dist loadgroup would run this stage apart from the rest of the scenario, "
                    f"without the earlier stages' saved values. Put xdist_group in the scenario's marks instead: every stage inherits it.",
                    location=location,
                )


def _template_key_diagnostics(test_data: dict[str, Any]) -> Iterator[Diagnostic]:
    """HTTPCHAIN029: ``{{ }}`` in a dict KEY, which is never substituted.

    `templates.walk` renders values only, so a templated key reaches the wire
    verbatim — a header or query parameter literally named ``{{ name }}``. It is
    equally invisible to `contains_template` and `extract_template_variables`,
    so without this check nothing anywhere would mention it: no error, no
    warning, just a wrong request.
    """

    def templated_keys(root: Any, root_location: str) -> Iterator[tuple[str, str, tuple[str | int, ...]]]:
        # Iterative, in document order: a recursive walk overflowed on a value
        # nested a few hundred levels deep. Each entry carries the key it sits
        # under, checked on visit, so a key is reported before its subtree,
        # with the location and path of the object holding it.
        Path = tuple[str | int, ...]
        pending: list[tuple[Any, str, Path, object, str, Path]] = [(root, root_location, (), None, "", ())]
        while pending:
            node, location, path, key, parent_location, parent_path = pending.pop()
            if isinstance(key, str) and re.search(TEMPLATE_PATTERN, key):
                yield key, parent_location, parent_path
            match node:
                case dict():
                    children = [(value, f"{location}.{k}" if location else str(k), (*path, k), k, location, path) for k, value in node.items()]
                case list():
                    children = [(item, f"{location}[{index}]", (*path, index), None, location, path) for index, item in enumerate(node)]
                case _:
                    continue
            pending.extend(reversed(children))

    for key, location, path in templated_keys(test_data, ""):
        if is_jmespath_expectations_position(path):
            # Only one that compiles gets here (a quoted string or field name):
            # the model refuses the rest, saying why.
            message = (
                f"Key {key!r} contains a template expression, but a verify.jmespath key is never rendered — JMESPath evaluates it as written. "
                f"Write the template in the value the key maps to."
            )
        else:
            message = (
                f"Key {key!r} contains a template expression, but only values are substituted — the key is sent literally. "
                f"Move the dynamic part into the value, or build the object in a user function."
            )
        yield diag(DiagnosticCode.TEMPLATE_IN_KEY, message, location=location or None)


def _template_kwargs_diagnostics(scenario: Scenario) -> Iterator[Diagnostic]:
    """HTTPCHAIN030: ``{{ }}`` inside a ``functions`` substitution's ``kwargs``,
    which `utils.process_substitutions` hands to ``wrap_function`` raw.

    Deliberate — the kwargs are the function's own defaults, not chain data — but
    it leaves the one template nothing ever renders, reaching the function as
    literal text. Like a templated key (029), no other check would mention it.

    Driven off the validated model, not the raw JSON: a request body may
    legitimately carry a key named "functions", and only the model tells a
    substitution step from one.
    """

    def offending(substitutions: Sequence[Substitution], location: str) -> Iterator[Diagnostic]:
        for step in substitutions:
            if not isinstance(step, FunctionsSubstitution):
                continue
            for alias, func_def in step.functions.items():
                if not isinstance(func_def, UserFunctionKwargs):
                    continue
                for name, value in func_def.kwargs.items():
                    if contains_template(value):
                        yield diag(
                            DiagnosticCode.TEMPLATE_IN_KWARGS,
                            f"Function '{alias}' kwarg '{name}' contains a template expression, but a functions substitution's kwargs are passed to the "
                            f"function unrendered — it arrives as literal text. Render the value in a 'vars' substitution and pass that variable where the "
                            f"alias is called, or resolve the value inside the function.",
                            location=location,
                        )

    yield from offending(scenario.substitutions, "substitutions")
    for i, stage in enumerate(scenario.stages):
        yield from offending(stage.substitutions, f"stages[{i}].substitutions")
        for k, step in enumerate(stage.response):
            if isinstance(step, SaveStep) and isinstance(step.save, SubstitutionsSave):
                yield from offending(step.save.substitutions, f"stages[{i}].response[{k}].save.substitutions")


def _parametrize_timing_diagnostics(scenario: Scenario) -> Iterator[Diagnostic]:
    """HTTPCHAIN025 (info): template parametrize values force scenario
    substitutions to resolve at collection time. Shares its predicate with the
    factory, so validator and runtime agree by construction."""
    for i, stage in enumerate(scenario.stages):
        if parametrize_values_contain_template(stage.parametrize):
            yield diag(
                DiagnosticCode.PARAMETRIZE_COLLECTION_RESOLUTION,
                f"Stage '{stage.name}' has template parametrize values: scenario-level substitutions for this scenario "
                f"resolve at collection time (pytest needs concrete parameter values), including any user functions they call",
                location=f"stages[{i}].parametrize",
            )
