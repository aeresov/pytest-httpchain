"""Scenario scope resolution: which names are visible to which phase.

The single encoding of the visibility rules, in three parts: name extraction
from a scenario (or its raw JSON), the static per-stage `StageScopes` used by
``validation`` and ``dataflow``, and the runtime ``ChainMap`` builders used by
``carrier``. Each context builder names the static phase it realizes, so a
change to either view is visibly a change to both.

The rules, in resolution order within a stage:

========================  ====================================================
Phase                     In scope
========================  ====================================================
``always_run``            fixtures, parametrize parameters, scenario
                          substitutions, earlier stages' saves
stage ``substitutions``   same as ``always_run``, plus PRIOR steps' names
                          (steps resolve strictly in order)
``parallel`` config       the above plus this stage's substitutions
request (per iteration)   the above plus ``foreach`` parameters
response (per iteration)  the above plus this stage's own saves and the
                          ``response`` metadata namespace
========================  ====================================================

Stage ``parametrize`` *values* are the exception: they resolve at collection
time against scenario substitutions only, which is why `StageScopes` exposes
``scenario_substitutions`` separately.
"""

import ast
import re
from collections import ChainMap
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from pytest_httpchain.models import (
    CombinationsParameter,
    FunctionsSubstitution,
    IndividualParameter,
    JMESPathSave,
    ParallelConfig,
    ParallelForeachConfig,
    Parameters,
    ResponseStep,
    SaveStep,
    Scenario,
    Stage,
    Substitutions,
    SubstitutionsSave,
    VarsSubstitution,
    normalize_list_input,
)
from pytest_httpchain.templates import TEMPLATE_BUILTINS, TEMPLATE_PATTERN

# The name under which response metadata is injected into response-step contexts.
RESPONSE_META_NAME = "response"

# Fields resolved once per scenario, against only the scenario substitutions.
SCENARIO_TEMPLATE_FIELDS = ("substitutions", "auth", "ssl")

# --------------------------------------------------------------------------- #
# Name extraction: which names a scenario fragment defines or references.
# --------------------------------------------------------------------------- #


def _extract_names_from_expr(expr: str) -> set[str]:
    """Free identifiers referenced by a Python expression.

    Comprehension targets and lambda parameters are local bindings, not context
    references. Falls back to a permissive regex if the expression doesn't parse.
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


def extract_template_variables(obj: Any) -> set[str]:
    """Variable names referenced by every ``{{ expr }}`` in a structure."""
    match obj:
        case str():
            names = {name for match in re.finditer(TEMPLATE_PATTERN, obj) for name in _extract_names_from_expr(match.group("expr"))}
            return names - TEMPLATE_BUILTINS
        case dict():
            return set().union(*(extract_template_variables(value) for value in obj.values()))
        case list():
            return set().union(*(extract_template_variables(item) for item in obj))
        case _:
            return set()


def substitution_names(substitutions: Substitutions) -> set[str]:
    """Names introduced by ``vars``/``functions`` substitution entries."""
    names: set[str] = set()
    for sub in substitutions:
        match sub:
            case VarsSubstitution():
                names.update(sub.vars.keys())
            case FunctionsSubstitution():
                names.update(sub.functions.keys())
    return names


def saved_in_step(response_step: ResponseStep) -> set[str]:
    """Names one response step saves. A ``user_functions`` save returns
    arbitrary keys, so it contributes none; verify steps save nothing."""
    if not isinstance(response_step, SaveStep):
        return set()
    match response_step.save:
        case JMESPathSave(jmespath=jmespath):
            return set(jmespath.keys())
        case SubstitutionsSave(substitutions=substitutions):
            return substitution_names(substitutions)
        case _:
            return set()


def saved_in_stage(stage: Stage) -> set[str]:
    """Names one stage's response steps save."""
    saved: set[str] = set()
    for response_step in stage.response:
        saved |= saved_in_step(response_step)
    return saved


def extract_saved_variables(scenario: Scenario) -> set[str]:
    """Names saved anywhere in the scenario."""
    saved_vars: set[str] = set()
    for stage in scenario.stages:
        saved_vars |= saved_in_stage(stage)
    return saved_vars


def parameter_names(params: Parameters | None) -> set[str]:
    """Names injected by parametrize/foreach entries. A template-string form
    defers its values to runtime and contributes no static names."""
    names: set[str] = set()
    for param in params or []:
        match param:
            case IndividualParameter(individual=individual):
                names.update(individual)
            case CombinationsParameter(combinations=combinations):
                if not isinstance(combinations, str):
                    for combo in combinations:
                        names.update(combo)
    return names


def foreach_parameter_names(parallel: ParallelConfig | None) -> set[str]:
    """Names injected per iteration by a ``parallel.foreach`` config."""
    match parallel:
        case ParallelForeachConfig(foreach=foreach):
            return parameter_names(foreach)
        case _:
            return set()


def extract_defined_variables(scenario: Scenario) -> set[str]:
    """Every name substitutions and parameters define, scenario-wide. The
    order-aware checks use `stage_scopes` instead."""
    defined_vars = substitution_names(scenario.substitutions)

    for stage in scenario.stages:
        defined_vars |= substitution_names(stage.substitutions)
        defined_vars |= parameter_names(stage.parametrize)
        defined_vars |= foreach_parameter_names(stage.parallel)

    return defined_vars


def raw_stages(test_data: dict[str, Any]) -> list[Any]:
    """Raw (pre-validation) stage bodies in declaration order, from either the
    list or the ``{name: stage}`` form."""
    raw = test_data.get("stages")
    if isinstance(raw, dict):
        return list(raw.values())
    if isinstance(raw, list):
        return raw
    return []


def raw_list_entries(raw: Any) -> list[Any]:
    """Raw entries of a list-or-mapping field in resolution order, so entry K
    pairs with the validated model's K.

    Shared by every raw-JSON reader of these fields — re-deriving the shape with
    a bare ``isinstance(..., list)`` silently drops the whole mapping form. The
    ``isinstance`` guard is what makes this different from the model's own use:
    the validator passes unrecognized input through for pydantic to reject,
    while a raw reader has no validator behind it and must degrade to ``[]``.
    """
    entries = normalize_list_input(raw)
    return list(entries) if isinstance(entries, list) else []


def _raw_substitution_entry_names(entry: Any) -> set[str]:
    if not isinstance(entry, dict):
        return set()
    names: set[str] = set()
    for key in ("vars", "functions"):
        value = entry.get(key)
        if isinstance(value, dict):
            names.update(value.keys())
    return names


def _raw_substitution_entry_templates(entry: Any) -> Any:
    """What the runtime renders at seed time: ``vars`` values and ``functions``
    import names; ``functions`` kwargs are passed to ``wrap_function`` raw."""
    if not isinstance(entry, dict):
        return None
    rendered: list[Any] = [entry.get("vars")]
    functions = entry.get("functions")
    if isinstance(functions, dict):
        for func_def in functions.values():
            if isinstance(func_def, str):
                rendered.append(func_def)
            elif isinstance(func_def, dict):
                rendered.append(func_def.get("name"))
    return rendered


def substitution_step_refs(raw_substitutions: Any) -> Iterator[tuple[set[str], frozenset[str]]]:
    """Walk raw substitution steps in resolution order, yielding
    ``(names the step references, names defined by PRIOR steps)``.

    Steps resolve strictly in order, so the prior-name set is both the scope
    addition (validation) and the shadow addition (dataflow) for that step.
    """
    prior_names: frozenset[str] = frozenset()
    for entry in raw_list_entries(raw_substitutions):
        yield extract_template_variables(_raw_substitution_entry_templates(entry)), prior_names
        prior_names |= frozenset(_raw_substitution_entry_names(entry))


# --------------------------------------------------------------------------- #
# Static scope model: per-stage, per-phase name availability.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class StageScopes:
    """Statically-known names visible to one stage, per resolution phase.

    Ingredients are stored separately so consumers can tell *why* a name is
    visible; the phase properties union them in the order the runtime layers its
    contexts, and each names its runtime twin.
    """

    scenario_substitutions: frozenset[str]
    scenario_fixtures: frozenset[str]
    stage_fixtures: frozenset[str]
    parametrize_params: frozenset[str]
    stage_substitutions: frozenset[str]
    foreach_params: frozenset[str]
    saves: frozenset[str]
    earlier_saves: frozenset[str]

    @property
    def always_run(self) -> frozenset[str]:
        """``always_run`` and the stage's own substitutions as they resolve.
        Twin: `stage_start_context`."""
        return self.scenario_substitutions | self.earlier_saves | self.scenario_fixtures | self.stage_fixtures | self.parametrize_params

    @property
    def pre_iteration(self) -> frozenset[str]:
        """The ``parallel`` config. Twin: `with_stage_substitutions`."""
        return self.always_run | self.stage_substitutions

    @property
    def request(self) -> frozenset[str]:
        """Request templates, per iteration. Twin: `iteration_context`."""
        return self.pre_iteration | self.foreach_params

    @property
    def response(self) -> frozenset[str]:
        """Response steps: the request scope plus this stage's own saves (treated
        as available to the whole response) and the ``response`` namespace.
        Twins: `response_step_context`, `with_saves`."""
        return self.request | self.saves | frozenset({RESPONSE_META_NAME})

    # Shadow sets: names layered ABOVE the global context in each phase, behind
    # which a same-named earlier save is unreadable.

    @property
    def always_run_shadows(self) -> frozenset[str]:
        """Also the base shadows of each substitution step, to which prior steps'
        names add cumulatively (see `substitution_step_refs`)."""
        return self.scenario_fixtures | self.stage_fixtures | self.parametrize_params

    @property
    def pre_iteration_shadows(self) -> frozenset[str]:
        return self.always_run_shadows | self.stage_substitutions

    @property
    def request_shadows(self) -> frozenset[str]:
        return self.pre_iteration_shadows | self.foreach_params


def stage_scopes(scenario: Scenario) -> list[StageScopes]:
    """Per-stage scopes in execution order. ``earlier_saves`` accumulates stage
    by stage, mirroring the runtime commit of saves after a stage passes."""
    scenario_substitutions = frozenset(substitution_names(scenario.substitutions))
    scenario_fixtures = frozenset(scenario.fixtures)

    scopes: list[StageScopes] = []
    earlier_saves: frozenset[str] = frozenset()
    for stage in scenario.stages:
        saves = frozenset(saved_in_stage(stage))
        scopes.append(
            StageScopes(
                scenario_substitutions=scenario_substitutions,
                scenario_fixtures=scenario_fixtures,
                stage_fixtures=frozenset(stage.fixtures),
                parametrize_params=frozenset(parameter_names(stage.parametrize)),
                stage_substitutions=frozenset(substitution_names(stage.substitutions)),
                foreach_params=frozenset(foreach_parameter_names(stage.parallel)),
                saves=saves,
                earlier_saves=earlier_saves,
            )
        )
        earlier_saves |= saves
    return scopes


# --------------------------------------------------------------------------- #
# Runtime context builders: the value-level twins of the phases above.
# --------------------------------------------------------------------------- #


def base_global_context(scenario_substitutions: Mapping[str, Any]) -> ChainMap[str, Any]:
    """The pristine global context; saves accumulate on top via `with_saves`."""
    return ChainMap(dict(scenario_substitutions))


def stage_start_context(global_context: ChainMap[str, Any], stage_fixtures: Mapping[str, Any]) -> ChainMap[str, Any]:
    """Fixtures (and parametrize parameters, which pytest injects through the
    same signature) over the global context."""
    return ChainMap(dict(stage_fixtures), global_context)


def with_stage_substitutions(stage_start: ChainMap[str, Any], stage_substitutions: Mapping[str, Any]) -> ChainMap[str, Any]:
    """The stage-local context: the base for every iteration."""
    return stage_start.new_child(dict(stage_substitutions))


def iteration_context(local_context: ChainMap[str, Any], iteration_params: Mapping[str, Any]) -> ChainMap[str, Any]:
    """Iteration parameters over the stage-local context."""
    return local_context.new_child(dict(iteration_params))


def response_step_context(iteration_ctx: ChainMap[str, Any], response_meta: Any) -> ChainMap[str, Any]:
    """The ``response`` namespace over the iteration context, layered last so a
    user variable cannot shadow it inside response steps."""
    return iteration_ctx.new_child({RESPONSE_META_NAME: response_meta})


def with_saves(context: ChainMap[str, Any], saves: Mapping[str, Any]) -> ChainMap[str, Any]:
    """Layer saved values over a context; later saves shadow earlier ones. Used
    both within a response and to commit a passed stage's saves."""
    return context.new_child(dict(saves))
