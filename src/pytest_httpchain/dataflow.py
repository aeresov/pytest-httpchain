"""Stage data-flow analysis for the ``show`` and ``graph`` CLI commands.

Reads the same per-stage scopes as the validator, but produces a graph instead
of diagnostics: what each stage saves, what it consumes, and the edges between.
"""

from typing import Any

from pydantic import BaseModel

from pytest_httpchain.models import Scenario
from pytest_httpchain.scoping import (
    RESPONSE_META_NAME,
    defined_names,
    extract_template_variables,
    raw_list_entries,
    raw_stages,
    saved_in_step,
    stage_scopes,
    substitution_names,
    substitution_step_templates,
)


def _consumed(refs: set[str], earlier_saves: frozenset[str], shadows: frozenset[str]) -> set[str]:
    """The subset of ``refs`` that reads an earlier stage's save: saved earlier
    and not masked by the phase's shadow set."""
    return {name for name in refs if name in earlier_saves and name not in shadows}


class DataFlowEdge(BaseModel):
    """A data dependency: ``vars`` saved by stage ``producer`` are referenced by
    stage ``consumer``. A name has more than one producer when its last writer
    before the consumer may skip: one edge from each writer back to the nearest
    that never skips (`analyze_dataflow`)."""

    producer: int
    consumer: int
    vars: list[str]


class StageFlow(BaseModel):
    """Per-stage data-flow summary. ``skip_if`` is the stage's as declared.
    ``may_skip`` says whether the stage may end without its saves while the
    chain goes on: a ``skip_if`` other than ``false``, or a ``skip``,
    ``skipif`` or ``xfail`` mark of its own (`scoping.SkipCause`). A later
    stage then reads the value an earlier stage saved under the same name, or
    finds none."""

    index: int
    name: str
    method: str
    url: str
    fixtures: list[str]
    marks: list[str]
    skip_if: bool | str
    may_skip: bool
    saves: list[str]
    consumes: list[str]


class DataFlow(BaseModel):
    """Whole-scenario data-flow graph."""

    stages: list[StageFlow]
    edges: list[DataFlowEdge]
    scenario_fixtures: list[str] = []
    scenario_vars: list[str] = []


def _producers(writers: list[int], stages: list[StageFlow]) -> list[int]:
    """The stages a consumer may read a name from, given its earlier writers in
    stage order: the last writer, as a later save shadows an earlier one — and
    while that writer may skip, the writer before it too, since a skipped
    stage leaves the chain running on the earlier value. The walk stops at the
    nearest writer that never skips; nearest first."""
    producers: list[int] = []
    for index in reversed(writers):
        producers.append(index)
        if not stages[index].may_skip:
            break
    return producers


def analyze_dataflow(scenario: Scenario, test_data: dict[str, Any]) -> DataFlow:
    """Build the stage data-flow graph for a validated scenario.

    A stage consumes a variable when one of its templates references a name an
    earlier stage saved and its own phase does not shadow — pre-response phases
    judged against the shadow sets `StageScopes` defines, response steps in
    order with the stage's own accumulated saves added as they land.
    ``parametrize`` values are excluded: they resolve against scenario scope,
    never saved values.

    A consumed name comes from its last writer before the consumer. When that
    writer may skip (a ``skip_if``, or a skip or xfail mark) it may have skipped
    with the chain going on, and the consumer then reads the writer before it:
    so the edges run from each writer back to, and including, the nearest one
    that never skips — ``graph`` draws those from a stage that may skip dotted.
    """
    raws = raw_stages(test_data)
    scopes = stage_scopes(scenario)
    defined = defined_names(scenario)

    stages: list[StageFlow] = []
    edges: list[DataFlowEdge] = []
    # Every stage that saves a name, in stage order. A re-saved variable is
    # attributed to its last writer before the consumer, matching the runtime
    # layering where a later save shadows an earlier one, and to the writers
    # before it back to one that cannot skip (`_producers`).
    writers: dict[str, list[int]] = {}

    for i, stage in enumerate(scenario.stages):
        scope = scopes[i]
        raw = raws[i] if i < len(raws) and isinstance(raws[i], dict) else {}

        consumes: set[str] = set()

        for templates, prior_sub_names in substitution_step_templates(raw.get("substitutions")):
            consumes |= _consumed(extract_template_variables(templates, defined=defined), scope.earlier_saves, scope.always_run_shadows | prior_sub_names)

        consumes |= _consumed(extract_template_variables(raw.get("skip_if"), defined=defined), scope.earlier_saves, scope.pre_iteration_shadows)
        consumes |= _consumed(extract_template_variables(raw.get("parallel"), defined=defined), scope.earlier_saves, scope.pre_iteration_shadows)
        consumes |= _consumed(extract_template_variables(raw.get("retry"), defined=defined), scope.earlier_saves, scope.pre_iteration_shadows)
        consumes |= _consumed(extract_template_variables(raw.get("request"), defined=defined), scope.earlier_saves, scope.request_shadows)
        # Response steps resolve in order, each save layering its names over the
        # context (the runtime's per-step with_saves): once a step re-saves a
        # name, later steps read this stage's fresh value, not the earlier
        # stage's — so accumulated own saves join the shadow set step by step.
        # The `response` namespace likewise shadows a same-named save.
        own_saves: frozenset[str] = frozenset()
        # raw_list_entries, not an isinstance(list) guard: the name-keyed mapping
        # form of `response` is first-class, and discarding it left every step's
        # raw text unread — so show/graph reported a consuming stage as
        # consuming nothing and dropped the dependency edge entirely.
        raw_response = raw_list_entries(raw.get("response"))
        for k, step in enumerate(stage.response):
            step_raw = raw_response[k] if k < len(raw_response) else None
            step_refs = extract_template_variables(step_raw, defined=defined) - {RESPONSE_META_NAME}
            consumes |= _consumed(step_refs, scope.earlier_saves, scope.request_shadows | own_saves)
            own_saves |= frozenset(saved_in_step(step))
        consumes |= _consumed(extract_template_variables(raw.get("always_run"), defined=defined), scope.earlier_saves, scope.always_run_shadows)

        by_producer: dict[int, list[str]] = {}
        for name in consumes:
            for producer in _producers(writers[name], stages):
                by_producer.setdefault(producer, []).append(name)
        for producer in sorted(by_producer):
            edges.append(DataFlowEdge(producer=producer, consumer=i, vars=sorted(by_producer[producer])))

        stages.append(
            StageFlow(
                index=i,
                name=stage.name,
                method=str(stage.request.method),
                url=stage.request.url,
                fixtures=sorted(stage.fixtures),
                marks=list(stage.marks),
                skip_if=stage.skip_if,
                may_skip=scope.skip_cause is not None,
                saves=sorted(scope.saves),
                consumes=sorted(consumes),
            )
        )

        for name in scope.saves:
            writers.setdefault(name, []).append(i)

    return DataFlow(
        stages=stages,
        edges=edges,
        scenario_fixtures=sorted(scenario.fixtures),
        scenario_vars=sorted(substitution_names(scenario.substitutions)),
    )
