"""Structured stage data-flow analysis for the ``show`` and ``graph`` CLI commands.

Consumes the same per-stage scope model (``scoping.stage_scopes``) as the
order-aware validator, but produces a graph instead of diagnostics: which
variables each stage saves, which it consumes from earlier stages, and the
producer -> consumer edges between stages.
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
)


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

    A stage *consumes* a variable when its request/response/substitutions/parallel
    templates reference a name saved by an earlier stage and not redefined locally
    (own substitutions, parametrize/foreach parameters, or stage/scenario fixtures).
    ``always_run`` references count too, but only fixtures and parametrize
    parameters shadow them — always_run resolves before stage substitutions exist.
    ``parametrize`` values are excluded — they resolve against scenario scope,
    never saved values.
    """
    raws = raw_stages(test_data)
    scopes = stage_scopes(scenario)

    stages: list[StageFlow] = []
    edges: list[DataFlowEdge] = []
    # The most recent stage (so far) that saved each name. A re-saved variable is
    # attributed to its LAST writer before the consumer, matching the runtime
    # ChainMap layering where a later save shadows an earlier one — not the first
    # writer, which is what the graph used to (incorrectly) draw.
    last_save_stage: dict[str, int] = {}

    for i, stage in enumerate(scenario.stages):
        scope = scopes[i]
        raw = raws[i] if i < len(raws) and isinstance(raws[i], dict) else {}

        refs: set[str] = set()
        for key in ("request", "substitutions", "parallel"):
            extract_template_variables(raw.get(key), refs)
        # Inside response steps the reserved `response` metadata namespace
        # shadows a same-named earlier save, so a `response` reference there is
        # NOT a data dependency on the earlier stage (in request/substitutions
        # templates it still is — no namespace exists in those scopes).
        refs |= extract_template_variables(raw.get("response")) - {RESPONSE_META_NAME}

        # Scenario fixtures count as local everywhere: at runtime they sit above
        # the global context in the ChainMap, shadowing any same-named save.
        local = scope.stage_substitutions | scope.parametrize_params | scope.foreach_params | scope.stage_fixtures | scope.scenario_fixtures
        consumes = {name for name in refs if name in scope.earlier_saves and name not in local}

        # always_run resolves before stage substitutions exist, so only fixtures
        # and parametrize parameters shadow an earlier save there.
        always_run_refs = extract_template_variables(raw.get("always_run"))
        always_run_local = scope.stage_fixtures | scope.parametrize_params | scope.scenario_fixtures
        consumes |= {name for name in always_run_refs if name in scope.earlier_saves and name not in always_run_local}

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
