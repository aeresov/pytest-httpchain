"""Structured stage data-flow analysis for the ``show`` and ``graph`` CLI commands.

Reuses the same extraction helpers as the order-aware validator
(``validation._dataflow_diagnostics``) but produces a graph model instead of
diagnostics: which variables each stage saves, which it consumes from earlier
stages, and the producer -> consumer edges between stages.
"""

from pathlib import Path
from typing import Any

import pytest_httpchain_jsonref.loader
from pydantic import BaseModel
from pytest_httpchain_models import Scenario

from pytest_httpchain.validation import (
    extract_template_variables,
    raw_stages,
    resolve_root_path,
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
    (own substitutions, parametrize/foreach parameters, or fixtures). ``parametrize``
    values are excluded — they resolve against scenario scope, never saved values.
    """
    raws = raw_stages(test_data)
    saves_by_stage = [saved_in_stage(stage) for stage in scenario.stages]

    first_save_stage: dict[str, int] = {}
    for i, saved in enumerate(saves_by_stage):
        for name in saved:
            first_save_stage.setdefault(name, i)

    stages: list[StageFlow] = []
    edges: list[DataFlowEdge] = []
    cumulative_saves: set[str] = set()

    for i, stage in enumerate(scenario.stages):
        raw = raws[i] if i < len(raws) and isinstance(raws[i], dict) else {}

        refs: set[str] = set()
        for key in ("request", "response", "substitutions", "parallel"):
            extract_template_variables(raw.get(key), refs)

        local = stage_defined_names(stage)
        consumes = {name for name in refs if name in cumulative_saves and name not in local}

        by_producer: dict[int, list[str]] = {}
        for name in consumes:
            by_producer.setdefault(first_save_stage[name], []).append(name)
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

    scenario_fixtures = sorted(f for f in test_data["fixtures"] if isinstance(f, str)) if isinstance(test_data.get("fixtures"), list) else []
    scenario_var_names = set(substitution_names(scenario.substitutions))
    if isinstance(test_data.get("vars"), dict):
        scenario_var_names |= {k for k in test_data["vars"] if isinstance(k, str)}

    return DataFlow(stages=stages, edges=edges, scenario_fixtures=scenario_fixtures, scenario_vars=sorted(scenario_var_names))


def load_scenario(path: Path, ref_parent_traversal_depth: int = 3) -> tuple[Scenario, dict[str, Any]]:
    """Load + ``$ref``-resolve + validate a scenario file.

    Returns ``(scenario, raw_test_data)``. Raises ``ReferenceResolverError``,
    ``json.JSONDecodeError`` or ``pydantic.ValidationError`` on failure — callers
    map these to user-facing errors.
    """
    test_data = pytest_httpchain_jsonref.loader.load_json(
        path,
        max_parent_traversal_depth=ref_parent_traversal_depth,
        root_path=resolve_root_path(path),
    )
    return Scenario.model_validate(test_data), test_data
