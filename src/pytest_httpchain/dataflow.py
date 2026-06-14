"""Structured stage data-flow analysis for the ``show`` and ``graph`` CLI commands.

Reuses the same extraction helpers as the order-aware validator
(``validation._dataflow_diagnostics``) but produces a graph model instead of
diagnostics: which variables each stage saves, which it consumes from earlier
stages, and the producer -> consumer edges between stages.
"""

from typing import Any

from pydantic import BaseModel
from pytest_httpchain_models import Scenario

from pytest_httpchain.validation import (
    _parameter_names,
    extract_template_variables,
    raw_stages,
    saved_in_stage,
    stage_defined_names,
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
    saves_by_stage = [saved_in_stage(stage) for stage in scenario.stages]
    scenario_fixture_names = set(scenario.fixtures)

    stages: list[StageFlow] = []
    edges: list[DataFlowEdge] = []
    cumulative_saves: set[str] = set()
    # The most recent stage (so far) that saved each name. A re-saved variable is
    # attributed to its LAST writer before the consumer, matching the runtime
    # ChainMap layering where a later save shadows an earlier one — not the first
    # writer, which is what the graph used to (incorrectly) draw.
    last_save_stage: dict[str, int] = {}

    for i, stage in enumerate(scenario.stages):
        raw = raws[i] if i < len(raws) and isinstance(raws[i], dict) else {}

        refs: set[str] = set()
        for key in ("request", "response", "substitutions", "parallel"):
            extract_template_variables(raw.get(key), refs)

        # Scenario fixtures count as local everywhere: at runtime they sit above
        # the global context in the ChainMap, shadowing any same-named save.
        local = stage_defined_names(stage) | scenario_fixture_names
        consumes = {name for name in refs if name in cumulative_saves and name not in local}

        # always_run resolves before stage substitutions exist, so only fixtures
        # and parametrize parameters shadow an earlier save there.
        always_run_refs = extract_template_variables(raw.get("always_run"))
        always_run_local = set(stage.fixtures) | _parameter_names(stage.parametrize) | scenario_fixture_names
        consumes |= {name for name in always_run_refs if name in cumulative_saves and name not in always_run_local}

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
                saves=sorted(saves_by_stage[i]),
                consumes=sorted(consumes),
            )
        )

        cumulative_saves |= saves_by_stage[i]
        for name in saves_by_stage[i]:
            last_save_stage[name] = i

    scenario_var_names = set(substitution_names(scenario.substitutions))

    return DataFlow(stages=stages, edges=edges, scenario_fixtures=sorted(scenario.fixtures), scenario_vars=sorted(scenario_var_names))
