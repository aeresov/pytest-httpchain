"""Stage data-flow analysis for the ``show`` and ``graph`` CLI commands.

Reads the same per-stage scopes as the validator, but produces a graph instead
of diagnostics: what each stage saves, what it consumes, and the edges between.
"""

from typing import Any

from pydantic import BaseModel

from pytest_httpchain.models import Scenario
from pytest_httpchain.scoping import (
    RESPONSE_META_NAME,
    extract_template_variables,
    raw_stages,
    stage_scopes,
    substitution_names,
    substitution_step_refs,
)


def _consumed(refs: set[str], earlier_saves: frozenset[str], shadows: frozenset[str]) -> set[str]:
    """The subset of ``refs`` that reads an earlier stage's save: saved earlier
    and not masked by the phase's shadow set."""
    return {name for name in refs if name in earlier_saves and name not in shadows}


class DataFlowEdge(BaseModel):
    """A data dependency: ``vars`` saved by stage ``producer`` are referenced by stage ``consumer``."""

    producer: int
    consumer: int
    vars: list[str]


class StageFlow(BaseModel):
    """Per-stage data-flow summary."""

    index: int
    name: str
    method: str
    url: str
    fixtures: list[str]
    marks: list[str]
    saves: list[str]
    consumes: list[str]


class DataFlow(BaseModel):
    """Whole-scenario data-flow graph."""

    stages: list[StageFlow]
    edges: list[DataFlowEdge]
    scenario_fixtures: list[str] = []
    scenario_vars: list[str] = []


def analyze_dataflow(scenario: Scenario, test_data: dict[str, Any]) -> DataFlow:
    """Build the stage data-flow graph for a validated scenario.

    A stage consumes a variable when one of its templates references a name an
    earlier stage saved and its own phase does not shadow — each phase judged
    against the shadow set `StageScopes` defines for it. ``parametrize`` values
    are excluded: they resolve against scenario scope, never saved values.
    """
    raws = raw_stages(test_data)
    scopes = stage_scopes(scenario)

    stages: list[StageFlow] = []
    edges: list[DataFlowEdge] = []
    # A re-saved variable is attributed to its last writer before the consumer,
    # matching the runtime layering where a later save shadows an earlier one.
    last_save_stage: dict[str, int] = {}

    for i, stage in enumerate(scenario.stages):
        scope = scopes[i]
        raw = raws[i] if i < len(raws) and isinstance(raws[i], dict) else {}

        consumes: set[str] = set()

        for entry_refs, prior_sub_names in substitution_step_refs(raw.get("substitutions")):
            consumes |= _consumed(entry_refs, scope.earlier_saves, scope.always_run_shadows | prior_sub_names)

        consumes |= _consumed(extract_template_variables(raw.get("parallel")), scope.earlier_saves, scope.pre_iteration_shadows)
        consumes |= _consumed(extract_template_variables(raw.get("request")), scope.earlier_saves, scope.request_shadows)
        # In response steps the `response` namespace shadows a same-named save,
        # so a reference to it there is not a dependency on the earlier stage.
        consumes |= _consumed(extract_template_variables(raw.get("response")) - {RESPONSE_META_NAME}, scope.earlier_saves, scope.request_shadows)
        consumes |= _consumed(extract_template_variables(raw.get("always_run")), scope.earlier_saves, scope.always_run_shadows)

        by_producer: dict[int, list[str]] = {}
        for name in consumes:
            by_producer.setdefault(last_save_stage[name], []).append(name)
        for producer in sorted(by_producer):
            edges.append(DataFlowEdge(producer=producer, consumer=i, vars=sorted(by_producer[producer])))

        stages.append(
            StageFlow(
                index=i,
                name=stage.name,
                method=str(stage.request.method),
                url=str(stage.request.url),
                fixtures=sorted(stage.fixtures),
                marks=list(stage.marks),
                saves=sorted(scope.saves),
                consumes=sorted(consumes),
            )
        )

        for name in scope.saves:
            last_save_stage[name] = i

    scenario_var_names = set(substitution_names(scenario.substitutions))

    return DataFlow(stages=stages, edges=edges, scenario_fixtures=sorted(scenario.fixtures), scenario_vars=sorted(scenario_var_names))
