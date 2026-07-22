"""Scenario scope resolution: which names are visible to which phase.

This module is the single encoding of the scenario visibility rules, shared by
the three places that previously each hand-encoded them:

* ``carrier`` builds the runtime ``ChainMap`` contexts with the
  context-builder functions (`base_global_context`, `stage_start_context`,
  `with_stage_substitutions`, `iteration_context`, `with_saves`);
* ``validation`` checks template references against the statically-known
  name sets (`StageScopes`) in its order-aware data-flow diagnostics;
* ``dataflow`` derives producer/consumer edges from the same sets.

Each context builder names the static phase it realizes, so the value-level
(runtime) and name-level (static) views of one rule sit side by side and a
change to either is visibly a change to both.

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

Scenario-level ``parametrize`` *values* are the exception: they resolve at
collection time against scenario substitutions only (see
``parametrize_values_contain_template``), which is why `StageScopes` exposes
``scenario_substitutions`` separately.
"""

import ast
import re
from collections import ChainMap
from collections.abc import Mapping
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
    SaveStep,
    Scenario,
    Stage,
    SubstitutionsSave,
    UserFunctionsSave,
    VarsSubstitution,
)
from pytest_httpchain.templates import TEMPLATE_BUILTINS, TEMPLATE_PATTERN

# The reserved name under which response metadata (status, reason, headers,
# elapsed_ms) is injected into every response step's template context.
RESPONSE_META_NAME = "response"

# --------------------------------------------------------------------------- #
# Name extraction: which names a scenario fragment defines or references.
# --------------------------------------------------------------------------- #


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
        match param:
            case IndividualParameter(individual=individual):
                names.update(individual)
            case CombinationsParameter(combinations=combinations):
                # A template-string form defers the combinations to runtime, so
                # it contributes no statically-known names.
                if not isinstance(combinations, str):
                    for combo in combinations:
                        names.update(combo)
    return names


def foreach_parameter_names(parallel: ParallelConfig | None) -> set[str]:
    """Names injected per iteration by a ``parallel.foreach`` config.

    Empty for ``repeat`` configs and for stages without a parallel config."""
    match parallel:
        case ParallelForeachConfig(foreach=foreach):
            return parameter_names(foreach)
        case _:
            return set()


def stage_defined_names(stage: Stage) -> set[str]:
    """Names available *within a single stage*: its substitutions, parametrize /
    foreach parameters, and its declared fixtures."""
    names = substitution_names(stage.substitutions)
    names |= parameter_names(stage.parametrize)
    names |= foreach_parameter_names(stage.parallel)
    names |= set(stage.fixtures)
    return names


def extract_defined_variables(scenario: Scenario) -> set[str]:
    """Extract variable names made available before/within templates (scenario-wide).

    Sources: ``vars`` and ``functions`` substitutions (scenario- and stage-level),
    plus parameter names injected by ``parametrize`` and ``parallel.foreach``. This
    is the *union* across the whole scenario, used for the fixture-conflict check
    and informational output; the order-aware checks use `stage_scopes` to compute
    availability per stage instead.
    """
    defined_vars: set[str] = set()

    defined_vars |= substitution_names(scenario.substitutions)

    for stage in scenario.stages:
        defined_vars |= substitution_names(stage.substitutions)
        defined_vars |= parameter_names(stage.parametrize)
        defined_vars |= foreach_parameter_names(stage.parallel)

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


def raw_substitution_entries(raw_substitutions: Any) -> list[Any]:
    """Raw substitution entries in resolution order.

    Substitutions may be authored as a list or as a name-keyed mapping (whose
    values may themselves be lists); this mirrors the model's list/mapping
    normalization (``_normalize_list_input``), so consumers walking the raw
    form see the same step order the runtime resolves in."""
    if isinstance(raw_substitutions, dict):
        entries: list[Any] = []
        for value in raw_substitutions.values():
            if isinstance(value, list):
                entries.extend(value)
            else:
                entries.append(value)
        return entries
    if isinstance(raw_substitutions, list):
        return list(raw_substitutions)
    return []


def raw_substitution_entry_names(entry: Any) -> set[str]:
    """Names a single raw substitution entry introduces (``vars``/``functions``
    keys) — the raw twin of `substitution_names` for one entry."""
    if not isinstance(entry, dict):
        return set()
    names: set[str] = set()
    for key in ("vars", "functions"):
        value = entry.get(key)
        if isinstance(value, dict):
            names.update(value.keys())
    return names


def raw_substitution_entry_templates(entry: Any) -> Any:
    """The part of a raw substitution entry the runtime renders at seed time:
    ``vars`` values only. ``functions`` kwargs are passed to ``wrap_function``
    raw (``utils.process_substitutions``) — a ``{{ }}`` inside them is dead
    text at seed time, not a context reference."""
    if isinstance(entry, dict):
        return entry.get("vars")
    return None


# --------------------------------------------------------------------------- #
# Static scope model: per-stage, per-phase name availability.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StageScopes:
    """Statically-known names visible to one stage, per resolution phase.

    The ingredient sets are stored separately (so consumers like ``dataflow``
    can distinguish *why* a name is visible); the phase properties union them
    in the order the runtime layers its contexts. Each phase property names
    the context-builder function that realizes it at runtime.
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
        """Scope of ``always_run`` and of the stage's own ``substitutions``
        while they are being resolved. Runtime twin: `stage_start_context`."""
        return self.scenario_substitutions | self.earlier_saves | self.scenario_fixtures | self.stage_fixtures | self.parametrize_params

    @property
    def pre_iteration(self) -> frozenset[str]:
        """Scope of the ``parallel`` config: resolved after stage substitutions,
        before any iteration. Runtime twin: `with_stage_substitutions`."""
        return self.always_run | self.stage_substitutions

    @property
    def request(self) -> frozenset[str]:
        """Scope of the request templates, per iteration. Runtime twin:
        `iteration_context`."""
        return self.pre_iteration | self.foreach_params

    @property
    def response(self) -> frozenset[str]:
        """Scope of the response steps: the request scope plus the stage's own
        saves and the ``response`` metadata namespace. Own saves are treated as
        available to the whole response (intra-response step ordering is
        approximated). Runtime twins: `response_step_context` per step, plus
        `with_saves` layered per save step."""
        return self.request | self.saves | frozenset({RESPONSE_META_NAME})

    # Shadow sets: the names layered ABOVE the global context in each phase — a
    # same-named earlier save is unreadable behind them. The per-phase mirror
    # of the scope properties, for consumers (dataflow) that need to know not
    # just what is visible but what MASKS an earlier save.

    @property
    def always_run_shadows(self) -> frozenset[str]:
        """Shadows while ``always_run`` resolves, and the base shadows for each
        stage-substitution step (prior steps' names add to these cumulatively;
        walk `raw_substitution_entries` for the step order)."""
        return self.scenario_fixtures | self.stage_fixtures | self.parametrize_params

    @property
    def pre_iteration_shadows(self) -> frozenset[str]:
        """Shadows in the ``parallel`` config scope: the stage's substitutions
        are fully resolved by then and layer above the global context."""
        return self.always_run_shadows | self.stage_substitutions

    @property
    def request_shadows(self) -> frozenset[str]:
        """Shadows in request/response templates (per iteration): everything
        above plus the ``foreach`` parameters."""
        return self.pre_iteration_shadows | self.foreach_params


def stage_scopes(scenario: Scenario) -> list[StageScopes]:
    """Compute per-stage `StageScopes` for every stage, in execution order.

    ``earlier_saves`` accumulates stage by stage, mirroring the runtime commit
    of a stage's saves into the global context (`with_saves`) after it passes.
    """
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
# The carrier calls these instead of layering ChainMaps inline, so the
# layering order is defined here, next to its static description.
# --------------------------------------------------------------------------- #


def base_global_context(scenario_substitutions: Mapping[str, Any]) -> ChainMap[str, Any]:
    """The pristine global context: resolved scenario substitutions only.

    Static twin: `StageScopes.scenario_substitutions` (saves accumulate on top
    via `with_saves` as stages pass)."""
    return ChainMap(dict(scenario_substitutions))


def stage_start_context(global_context: ChainMap[str, Any], stage_fixtures: Mapping[str, Any]) -> ChainMap[str, Any]:
    """Context at stage start: fixtures (and parametrize parameters, which
    pytest injects through the same method signature) over the global context.

    Evaluates ``always_run`` and resolves the stage's substitutions.
    Static twin: `StageScopes.always_run`."""
    return ChainMap(dict(stage_fixtures), global_context)


def with_stage_substitutions(stage_start: ChainMap[str, Any], stage_substitutions: Mapping[str, Any]) -> ChainMap[str, Any]:
    """The stage-local context: resolved stage substitutions over the stage-start
    context. Resolves the ``parallel`` config and is the base for iterations.

    Static twin: `StageScopes.pre_iteration`."""
    return stage_start.new_child(dict(stage_substitutions))


def iteration_context(local_context: ChainMap[str, Any], iteration_params: Mapping[str, Any]) -> ChainMap[str, Any]:
    """Per-iteration context: ``foreach``/``repeat`` iteration parameters over the
    stage-local context. Resolves the request and response templates.

    Static twin: `StageScopes.request`."""
    return local_context.new_child(dict(iteration_params))


def response_step_context(iteration_ctx: ChainMap[str, Any], response_meta: Any) -> ChainMap[str, Any]:
    """Per-response-step context: the ``response`` metadata namespace over the
    iteration context (and over any earlier save steps' values layered onto
    it), so it cannot be shadowed by a user variable within response steps.

    Static twin: `StageScopes.response` (which includes `RESPONSE_META_NAME`)."""
    return iteration_ctx.new_child({RESPONSE_META_NAME: response_meta})


def with_saves(context: ChainMap[str, Any], saves: Mapping[str, Any]) -> ChainMap[str, Any]:
    """Layer saved values over a context: later saves shadow earlier ones.

    Used both for intra-response accumulation (each save step's results are
    visible to the steps after it — static twin: `StageScopes.response`) and
    for committing a passed stage's saves into the global context (static
    twin: `StageScopes.earlier_saves` of the following stages)."""
    return context.new_child(dict(saves))
