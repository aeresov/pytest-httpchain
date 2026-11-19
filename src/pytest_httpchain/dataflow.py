"""Stage data-flow analysis for the ``show`` and ``graph`` CLI commands.

Reads the same per-stage scopes as the validator, but produces a graph instead
of diagnostics: what each stage saves, what it consumes, and the edges between.
"""

from collections.abc import Set as AbstractSet
from typing import Any, Literal

from pydantic import BaseModel

from pytest_httpchain.models import Scenario
from pytest_httpchain.scoping import (
    connection_lifecycles,
    connection_rendered_fields,
    defined_names,
    extract_template_variables,
    raw_stages,
    stage_scopes,
    stage_templates,
    substitution_names,
)


def _consumed(refs: set[str], earlier_saves: frozenset[str], shadows: AbstractSet[str]) -> set[str]:
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


class ConnectionEdge(BaseModel):
    """A connection dependency: stage ``user`` talks over the session of
    ``connection`` that stage ``opener`` may have opened (one edge per
    possible opener, `scoping.connection_lifecycles`)."""

    opener: int
    user: int
    connection: str


class StageFlow(BaseModel):
    """Per-stage data-flow summary. ``skip_if`` is the stage's as declared.
    ``may_skip`` says whether the stage may end without its saves while the
    chain goes on: a ``skip_if`` other than ``false``, or a ``skip``,
    ``skipif`` or ``xfail`` mark of its own (`scoping.SkipCause`). A later
    stage then reads the value an earlier stage saved under the same name, or
    finds none.

    ``kind`` is what the stage does: an HTTP ``request``, or a step on
    ``connection``, whose ``actions`` are those it sets, in the order it takes
    them. A connection stage's ``method`` is ``CONNECTION`` and its ``url``
    the connection's name, so both stay text."""

    index: int
    name: str
    kind: Literal["request", "connection"] = "request"
    connection: str | None = None
    actions: list[str] = []
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
    connection_edges: list[ConnectionEdge] = []
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

    A stage consumes a variable when one of its templates
    (`scoping.stage_templates`) references a name an earlier stage saved and
    its own phase does not shadow — each phase judged against its shadow set
    (`scoping.StageScopes.shadows`), substitution and response steps in order
    with the names the steps before them define or save added as they land.
    ``parametrize`` values are excluded: they resolve against scenario scope,
    never saved values.

    A consumed name comes from its last writer before the consumer. When that
    writer may skip (a ``skip_if``, or a skip or xfail mark) it may have skipped
    with the chain going on, and the consumer then reads the writer before it:
    so the edges run from each writer back to, and including, the nearest one
    that never skips — ``graph`` draws those from a stage that may skip dotted.

    A connection stage consumes what its step renders
    (`scoping.connection_step_templates`), and each stage that may open a
    connection what the connection's ``params`` read, against its scope for
    them (`scoping.StageScopes.connection_params`, beneath only the
    scenario's fixtures). ``connection_edges`` run from each stage that may
    have opened the session a later stage uses to that stage.
    """
    raws = raw_stages(test_data)
    scopes = stage_scopes(scenario)
    defined = defined_names(scenario)
    lifecycles = connection_lifecycles(scenario, scopes)
    raw_connections = test_data.get("connections")
    if not isinstance(raw_connections, dict):
        raw_connections = {}
    # What each stage that may open a connection renders of it.
    opened_params: dict[int, list[Any]] = {}
    for name, lifecycle in lifecycles.items():
        rendered = [value for _, value in connection_rendered_fields(raw_connections.get(name))]
        for opener in lifecycle.openers:
            opened_params.setdefault(opener, []).extend(rendered)

    stages: list[StageFlow] = []
    edges: list[DataFlowEdge] = []
    # Every stage that saves a name, in stage order. A re-saved variable is
    # attributed to its last writer before the consumer, matching the runtime
    # layering where a later save shadows an earlier one, and to the writers
    # before it back to one that cannot skip (`_producers`).
    writers: dict[str, list[int]] = {}

    for i, stage in enumerate(scenario.stages):
        scope = scopes[i]
        shadows = scope.shadows()
        raw = raws[i] if i < len(raws) and isinstance(raws[i], dict) else {}

        consumes: set[str] = set()

        # The templates the validator reads, each in its phase: what one
        # sees besides the phase's names (`scoping.StageTemplate.extra`:
        # what the steps before it define or save, or ``sent``) hides an
        # earlier save from it, as the phase's own names do.
        for _, phase, templates, extra in stage_templates(stage, raw):
            if phase is not None:
                consumes |= _consumed(extract_template_variables(templates, defined=defined), scope.earlier_saves, shadows[phase] | extra)
        consumes |= _consumed(extract_template_variables(opened_params.get(i), defined=defined), scope.earlier_saves, scope.scenario_fixtures)

        by_producer: dict[int, list[str]] = {}
        for name in consumes:
            for producer in _producers(writers[name], stages):
                by_producer.setdefault(producer, []).append(name)
        for producer in sorted(by_producer):
            edges.append(DataFlowEdge(producer=producer, consumer=i, vars=sorted(by_producer[producer])))

        # A connection stage shows as its connection, so method and url stay
        # text for show's JSON readers.
        if stage.request is not None:
            method, url = str(stage.request.method), stage.request.url
        else:
            assert stage.connection is not None, "a stage without a request has a connection step"
            method, url = "CONNECTION", stage.connection.name
        stages.append(
            StageFlow(
                index=i,
                name=stage.name,
                kind="connection" if stage.connection is not None else "request",
                connection=stage.connection.name if stage.connection is not None else None,
                actions=list(stage.connection.actions) if stage.connection is not None else [],
                method=method,
                url=url,
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

    connection_edges = [
        ConnectionEdge(opener=opener, user=user, connection=name) for name, lifecycle in lifecycles.items() for user, openers in lifecycle.sessions.items() for opener in openers
    ]
    connection_edges.sort(key=lambda edge: (edge.user, edge.opener, edge.connection))

    return DataFlow(
        stages=stages,
        edges=edges,
        connection_edges=connection_edges,
        scenario_fixtures=sorted(scenario.fixtures),
        scenario_vars=sorted(substitution_names(scenario.substitutions)),
    )
