"""Stage data-flow analysis for the ``show`` and ``graph`` CLI commands.

Reads the same per-stage scopes as the validator, but produces a graph instead
of diagnostics: what each stage saves, what it consumes, and the edges between.
"""

from typing import Any, Literal

from pydantic import BaseModel

from pytest_httpchain.models import ConnectionStep, Scenario
from pytest_httpchain.scoping import (
    SENT_NAME,
    connection_lifecycles,
    connection_rendered_fields,
    connection_step_templates,
    defined_names,
    extract_template_variables,
    raw_stages,
    response_step_templates,
    stage_scopes,
    substitution_names,
    substitution_step_templates,
)


def _sets(step: ConnectionStep, action: str) -> bool:
    """Whether a connection step takes ``action`` (one of `ConnectionStep.ACTIONS`)."""
    value = getattr(step, action)
    return value is not None and value is not False


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
        raw = raws[i] if i < len(raws) and isinstance(raws[i], dict) else {}

        consumes: set[str] = set()

        for templates, prior_sub_names in substitution_step_templates(raw.get("substitutions")):
            consumes |= _consumed(extract_template_variables(templates, defined=defined), scope.earlier_saves, scope.always_run_shadows | prior_sub_names)

        consumes |= _consumed(extract_template_variables(raw.get("skip_if"), defined=defined), scope.earlier_saves, scope.pre_iteration_shadows)
        consumes |= _consumed(extract_template_variables(raw.get("parallel"), defined=defined), scope.earlier_saves, scope.pre_iteration_shadows)
        consumes |= _consumed(extract_template_variables(raw.get("retry"), defined=defined), scope.earlier_saves, scope.pre_iteration_shadows)
        consumes |= _consumed(extract_template_variables(raw.get("request"), defined=defined), scope.earlier_saves, scope.request_shadows)
        if stage.connection is not None:
            send, after_send = connection_step_templates(raw.get("connection"))
            sent = frozenset({SENT_NAME}) if stage.connection.send is not None else frozenset()
            consumes |= _consumed(extract_template_variables(send, defined=defined), scope.earlier_saves, scope.request_shadows)
            consumes |= _consumed(extract_template_variables(after_send, defined=defined), scope.earlier_saves, scope.request_shadows | sent)
        consumes |= _consumed(extract_template_variables(opened_params.get(i), defined=defined), scope.earlier_saves, scope.scenario_fixtures)
        # The same order-aware iterator the validator uses also sees prior
        # entries inside a substitutions-save step. Those entries resolve in
        # sequence, and only their rendered values can consume an earlier save.
        for templates, own_saves in response_step_templates(stage, raw.get("response")):
            step_refs = extract_template_variables(templates, defined=defined) - scope.response_names
            consumes |= _consumed(step_refs, scope.earlier_saves, scope.request_shadows | own_saves)
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
                kind="connection" if stage.connection is not None else "request",
                connection=stage.connection.name if stage.connection is not None else None,
                actions=[action for action in ConnectionStep.ACTIONS if stage.connection is not None and _sets(stage.connection, action)],
                # A connection stage shows as its connection, so the field
                # stays text for show's JSON readers.
                method=str(stage.request.method) if stage.request is not None else "CONNECTION",
                url=stage.request.url if stage.request is not None else stage.connection.name if stage.connection is not None else "",
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
        ConnectionEdge(opener=opener, user=user, connection=name)
        for name, lifecycle in lifecycles.items()
        for user, openers in sorted(lifecycle.sessions.items())
        for opener in sorted(openers)
    ]
    connection_edges.sort(key=lambda edge: (edge.user, edge.opener, edge.connection))

    return DataFlow(
        stages=stages,
        edges=edges,
        connection_edges=connection_edges,
        scenario_fixtures=sorted(scenario.fixtures),
        scenario_vars=sorted(substitution_names(scenario.substitutions)),
    )
