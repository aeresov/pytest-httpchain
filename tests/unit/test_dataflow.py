"""Unit tests for analyze_dataflow: which earlier saves each stage consumes,
and the producer -> consumer edges drawn from them."""

import pytest

from pytest_httpchain.dataflow import analyze_dataflow
from pytest_httpchain.models import Scenario
from tests.unit.helpers import BEYOND_RECURSION_LIMIT, nested


def _scenario(stages, **top):
    data = {"stages": stages, **top}
    return Scenario.model_validate(data), data


def _stage(name: str, **fields) -> dict:
    return {"name": name, "request": {"url": "https://x.test/"}, "response": [{"verify": {"status": 200}}], **fields}


def _producer(name: str = "producer", **saves: str) -> dict:
    return _stage(name, request={"url": "https://x.test/", "method": "POST"}, response=[{"save": {"jmespath": saves}}])


def _edges(flow) -> list[dict]:
    return [e.model_dump() for e in flow.edges]


@pytest.mark.parametrize(
    ("consumer", "expected"),
    [
        pytest.param({"request": {"url": "https://x.test/b"}}, [], id="independent"),
        pytest.param({"request": {"url": "https://x.test/{{ x }}"}}, ["x"], id="request"),
        pytest.param({"request": {"url": "https://x.test/{{ x }}", "headers": {"a": "{{ y }}"}}}, ["x", "y"], id="several-vars-one-edge"),
        # A built-in auth renders per iteration, like the rest of the request.
        pytest.param({"request": {"url": "https://x.test/", "auth": {"bearer": "{{ x }}"}}}, ["x"], id="request-auth-bearer"),
        pytest.param({"substitutions": [{"vars": {"x": "local"}}], "request": {"url": "https://x.test/{{ x }}"}}, [], id="stage-substitution-shadows"),
        # always_run resolves before stage substitutions exist, so it reads the
        # earlier save even when a stage substitution reuses the name.
        pytest.param({"substitutions": [{"vars": {"x": "local"}}], "always_run": "{{ x }}"}, ["x"], id="always-run"),
        # skip_if resolves after them: a stage substitution shadows the save there.
        pytest.param({"substitutions": [{"vars": {"x": "local"}}], "skip_if": "{{ x == y }}"}, ["y"], id="skip-if"),
        # parametrize values resolve against scenario scope, never saves.
        pytest.param({"parametrize": [{"individual": {"n": ["{{ x }}"]}}], "request": {"url": "https://x.test/{{ n }}"}}, [], id="parametrize-values"),
        # Stage substitutions and the parallel config resolve before any
        # iteration exists, so a foreach parameter cannot shadow a save there.
        pytest.param(
            {"substitutions": [{"vars": {"z": "{{ x }}"}}], "parallel": {"foreach": [{"individual": {"x": [1, 2]}}]}, "request": {"url": "https://x.test/{{ x }}/{{ z }}"}},
            ["x"],
            id="foreach-param-vs-substitutions",
        ),
        pytest.param({"parallel": {"foreach": [{"individual": {"x": [1, 2]}}], "max_concurrency": "{{ x }}"}}, ["x"], id="foreach-param-vs-parallel-config"),
        pytest.param({"parallel": {"repeat": 2, "collect_saves": "{{ x }}"}}, ["x"], id="parallel-collect-saves"),
        # retry resolves with the parallel config: a stage substitution
        # shadows the save there, a foreach parameter does not.
        pytest.param({"substitutions": [{"vars": {"x": 3}}], "retry": {"attempts": "{{ x }}", "delay": "{{ y }}"}}, ["y"], id="retry"),
        pytest.param({"parallel": {"foreach": [{"individual": {"x": [1, 2]}}]}, "retry": {"attempts": "{{ x }}"}}, ["x"], id="foreach-param-vs-retry"),
        # Substitution steps resolve in order: a PRIOR step's name shadows the
        # save for later steps, a LATER step's does not.
        pytest.param({"substitutions": [{"vars": {"x": "local"}}, {"vars": {"z": "{{ x }}"}}]}, [], id="prior-substitution-step-shadows"),
        pytest.param({"substitutions": [{"vars": {"z": "{{ x }}"}}, {"vars": {"x": "local"}}]}, ["x"], id="later-substitution-step"),
        # Response steps resolve in order with per-step with_saves layering:
        # once the stage re-saves a name, later steps read its own fresh value.
        pytest.param({"response": [{"save": {"jmespath": {"x": "b"}}}, {"verify": {"expressions": ["{{ x != '' }}"]}}]}, [], id="own-resave-shadows-later-steps"),
        pytest.param({"response": [{"verify": {"expressions": ["{{ x != '' }}"]}}, {"save": {"jmespath": {"x": "b"}}}]}, ["x"], id="reference-before-own-resave"),
        # A regex save's names layer the same way, and its patterns are rendered.
        pytest.param({"response": [{"save": {"regex": {"x": "(b)"}}}, {"verify": {"expressions": ["{{ x != '' }}"]}}]}, [], id="own-regex-resave-shadows-later-steps"),
        pytest.param(
            {"response": [{"save": {"substitutions": [{"vars": {"x": "local"}}, {"vars": {"z": "{{ x }}"}}]}}]},
            [],
            id="prior-substitution-within-save-step-shadows",
        ),
        pytest.param(
            {"response": [{"save": {"substitutions": [{"vars": {"z": "{{ x }}"}}, {"vars": {"x": "local"}}]}}]},
            ["x"],
            id="later-substitution-within-save-step-does-not-shadow",
        ),
        pytest.param({"response": [{"save": {"regex": {"z": "id={{ x }}", "w": {"pattern": "(a)", "group": "{{ y }}"}}}}]}, ["x", "y"], id="regex-save-templates"),
        # An entry of a status list takes a template of its own.
        pytest.param({"response": [{"verify": {"status": ["{{ x }}", 304]}}]}, ["x"], id="verify-status-list-entry"),
        # A verify.jmespath value and a matcher operand are rendered; the
        # expression, a key, never is.
        pytest.param({"response": [{"verify": {"jmespath": {"id": "{{ x }}", "tags": {"contains": "{{ y }}"}}}}]}, ["x", "y"], id="verify-jmespath-values"),
        pytest.param({"response": [{"verify": {"jmespath": {"'{{ x }}'": 1}}}]}, [], id="verify-jmespath-key"),
        # A recursive reference search crashed `show` and `graph` on a value a
        # few hundred levels deep.
        pytest.param({"request": {"url": "https://x.test/", "params": {"p": nested("{{ x }}", BEYOND_RECURSION_LIMIT)}}}, ["x"], id="reference-nested-past-the-recursion-limit"),
    ],
)
def test_consumer_of_earlier_saves(consumer, expected):
    flow = analyze_dataflow(*_scenario([_producer(x="a", y="b"), _stage("consumer", **consumer)]))
    assert flow.stages[1].consumes == expected
    assert _edges(flow) == ([{"producer": 0, "consumer": 1, "vars": expected}] if expected else [])


def test_scenario_fixture_shadows_save():
    """At runtime a scenario fixture shadows a same-named save in every stage,
    so the reference resolves to the fixture — no producer->consumer edge."""
    flow = analyze_dataflow(*_scenario([_producer(token="t"), _stage("b", request={"url": "https://x.test/{{ token }}"})], fixtures=["token"]))
    assert flow.stages[1].consumes == []
    assert flow.edges == []


def test_same_stage_save_and_use_no_self_edge():
    flow = analyze_dataflow(*_scenario([_stage("a", response=[{"save": {"jmespath": {"token": "t"}}}, {"verify": {"expressions": ["{{ token != '' }}"]}}])]))
    assert flow.stages[0].saves == ["token"]
    assert flow.stages[0].consumes == []
    assert flow.edges == []


def test_regex_save_names_are_saves_and_feed_edges():
    """``show`` and ``graph`` list a regex save's names as a JMESPath save's,
    and draw the edge to the stage reading them."""
    producer = _stage("page", response=[{"save": {"regex": {"csrf": 'value="([^"]+)"', "ids": {"pattern": "id=(\\d+)", "all": True}}}}])
    flow = analyze_dataflow(*_scenario([producer, _stage("submit", request={"url": "https://x.test/", "headers": {"X-CSRF": "{{ csrf }}"}})]))
    assert flow.stages[0].saves == ["csrf", "ids"]
    assert _edges(flow) == [{"producer": 0, "consumer": 1, "vars": ["csrf"]}]


def test_collected_saves_are_the_stage_saves_and_feed_edges():
    """A stage with ``parallel.collect_saves`` saves the same names, each a
    list once it has run: ``show`` and ``graph`` list them as the stage's, and
    draw the edge to a later stage whose foreach goes through one."""
    create = _producer("create", created_ids="id") | {"parallel": {"repeat": 3, "collect_saves": True}}
    delete = _stage("delete", parallel={"foreach": [{"individual": {"id": "{{ created_ids }}"}}]}, request={"url": "https://x.test/{{ id }}", "method": "DELETE"})
    flow = analyze_dataflow(*_scenario([create, delete]))
    assert flow.stages[0].saves == ["created_ids"]
    assert _edges(flow) == [{"producer": 0, "consumer": 1, "vars": ["created_ids"]}]


def test_stats_as_is_a_save_of_its_stage_and_feeds_edges():
    """``show`` and ``graph`` list a stage's ``parallel.stats_as`` among its
    saves, and draw the edge to a later stage reading it. The stage's own
    templates cannot read its stats, which exist only once every iteration
    has ended: a reference there consumes an earlier stage's save of the name."""
    load = _stage(
        "load",
        parallel={"repeat": 3, "stats_as": "stats"},
        response=[{"verify": {"expressions": ["{{ stats.passed > 0 }}"]}}, {"save": {"jmespath": {"id": "id"}}}],
    )
    report = _stage("report", request={"url": "https://x.test/?p95={{ stats.p95_ms }}"})
    flow = analyze_dataflow(*_scenario([_producer(stats="s"), load, report]))
    assert flow.stages[1].saves == ["id", "stats"]
    assert flow.stages[1].consumes == ["stats"]
    assert _edges(flow) == [{"producer": 0, "consumer": 1, "vars": ["stats"]}, {"producer": 1, "consumer": 2, "vars": ["stats"]}]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # The read is the save's: the runtime looks up the user's names first.
        pytest.param("https://x.test/?since={{ timestamp }}", ["timestamp"], id="read"),
        # A saved value is no function, so the call reaches the built-in.
        pytest.param("https://x.test/?at={{ timestamp() }}", [], id="call"),
    ],
)
def test_save_named_like_a_builtin_is_consumed_where_read(url, expected):
    """A save named like a built-in helper (timestamp, now, quote, ...) still
    draws its edge: dropping every built-in's name from the references left it
    out of `show` and `graph`."""
    flow = analyze_dataflow(*_scenario([_producer(timestamp="meta.ts"), _stage("consumer", request={"url": url})]))
    assert flow.stages[1].consumes == expected
    assert _edges(flow) == ([{"producer": 0, "consumer": 1, "vars": expected}] if expected else [])


def test_call_under_a_saved_name_consumes_nothing_where_a_fixture_shares_it():
    """A stage elsewhere requesting a `timestamp` fixture makes the name a
    possible function, but where the save is in scope its value is no function:
    the call reaches the built-in and reads nothing from the producer."""
    consumer = _stage("consumer", request={"url": "https://x.test/?at={{ timestamp() }}"})
    flow = analyze_dataflow(*_scenario([_producer(timestamp="meta.ts"), consumer, _stage("clock", fixtures=["timestamp"])]))
    assert flow.stages[1].consumes == []
    assert _edges(flow) == []


def test_stage_that_may_skip_is_marked():
    """``show`` and ``graph`` mark a stage whose skip_if may skip it, and so
    leave its saves unmade: its skip_if as declared, ``false`` when it has none."""
    stages = [{**_producer("maybe", x="v"), "skip_if": "{{ flag }}"}, _stage("never", skip_if=True), _stage("reader", request={"url": "https://x.test/{{ x }}"})]
    flow = analyze_dataflow(*_scenario(stages))
    assert [stage.skip_if for stage in flow.stages] == ["{{ flag }}", True, False]
    assert _edges(flow) == [{"producer": 0, "consumer": 2, "vars": ["x"]}]


def test_may_skip_covers_skip_if_and_the_stages_own_marks():
    """Not the scenario's marks: they skip the reader along with the writer."""
    stages = [_stage("a", skip_if="{{ flag }}"), _stage("b", marks=["xfail"]), _stage("c", marks=["slow"]), _stage("d")]
    flow = analyze_dataflow(*_scenario(stages, marks=["skip"]))
    assert [stage.may_skip for stage in flow.stages] == [True, True, False, False]


def _maybe(name: str, **saves: str) -> dict:
    """A producer with a skip_if: it may skip, the chain going on without its saves."""
    return {**_producer(name, **saves), "skip_if": "{{ flag }}"}


@pytest.mark.parametrize(
    ("writers", "producers"),
    [
        # The case skip_if is for: a conditional refresh re-saves the login's
        # token. When it skips, the reader runs on the login's: both are edges.
        pytest.param([_producer("login", x="v"), _maybe("refresh", x="v")], [0, 1], id="unconditional-then-skippable"),
        # The walk goes on through every skippable writer, to the nearest that
        # never skips, and no further: the one before it is always shadowed.
        pytest.param([_producer("a", x="v"), _producer("b", x="v"), _maybe("c", x="v"), _maybe("d", x="v")], [1, 2, 3], id="stops-at-nearest-unconditional"),
        # Only skippable writers: every one may be the source (or none, HTTPCHAIN003).
        pytest.param([_maybe("a", x="v"), _maybe("b", x="v")], [0, 1], id="all-skippable"),
        # A skippable writer before the last unconditional one is shadowed by it.
        pytest.param([_maybe("a", x="v"), _producer("b", x="v")], [1], id="skippable-then-unconditional"),
        # `skip_if: false` never skips.
        pytest.param([_producer("a", x="v"), {**_producer("b", x="v"), "skip_if": False}], [1], id="skip-if-false"),
        # A stage's own skip or xfail mark leaves the chain going as a skip_if skip does.
        pytest.param([_producer("login", x="v"), {**_producer("refresh", x="v"), "marks": ["skip"]}], [0, 1], id="skip-mark"),
        pytest.param([_producer("login", x="v"), {**_producer("refresh", x="v"), "marks": ["xfail"]}], [0, 1], id="xfail-mark"),
        # A mark whose condition is false never skips.
        pytest.param([_producer("a", x="v"), {**_producer("b", x="v"), "marks": ["skipif(False, reason='on')"]}], [1], id="inactive-mark"),
    ],
)
def test_a_name_whose_last_writer_may_skip_comes_from_the_writers_before(writers, producers):
    """A re-saved name is attributed to its last writer, but a skipped stage
    leaves the chain running on the earlier value: while the writer may skip (a
    skip_if, or a skip, skipif or xfail mark of its own), the one before it is a
    producer too, back to one that never skips."""
    reader = _stage("reader", request={"url": "https://x.test/{{ x }}"})
    flow = analyze_dataflow(*_scenario([*writers, reader]))
    assert flow.stages[-1].consumes == ["x"]
    assert _edges(flow) == [{"producer": p, "consumer": len(writers), "vars": ["x"]} for p in producers]


def test_walk_back_is_per_name():
    """Each name walks its own writers: one edge per producer, carrying the
    names it may be the source of."""
    stages = [_producer("login", x="v", y="v"), _maybe("refresh", x="v"), _stage("reader", request={"url": "https://x.test/{{ x }}/{{ y }}"})]
    flow = analyze_dataflow(*_scenario(stages))
    assert _edges(flow) == [{"producer": 0, "consumer": 2, "vars": ["x", "y"]}, {"producer": 1, "consumer": 2, "vars": ["x"]}]


def test_latest_producer_selected():
    """A re-saved variable is attributed to its LAST writer before the consumer
    (stage b), matching runtime ChainMap layering — not the first (M10)."""
    flow = analyze_dataflow(*_scenario([_producer("a", x="v"), _producer("b", x="v"), _stage("c", request={"url": "https://x.test/{{ x }}"})]))
    assert flow.stages[2].consumes == ["x"]
    assert _edges(flow) == [{"producer": 1, "consumer": 2, "vars": ["x"]}]


def test_saved_response_name_not_consumed_in_response_steps():
    """The reserved `response` metadata namespace shadows a same-named earlier
    save inside response steps, so a `response` reference there is not a data
    dependency; in a request template it still is."""
    flow = analyze_dataflow(
        *_scenario(
            [
                _producer(response="body.data"),
                _stage("verify_meta", response=[{"verify": {"expressions": ["{{ response.status == 200 }}"]}}]),
                _stage("request_consumer", request={"url": "https://x.test/", "headers": {"x-d": "{{ response }}"}}),
            ]
        )
    )
    assert flow.stages[1].consumes == []
    assert flow.stages[2].consumes == ["response"]


def test_mapping_form_response_steps_are_analyzed():
    """The name-keyed `response` mapping form is first-class. Re-deriving the
    raw shape with a bare isinstance(list) discarded it, so every reference
    inside a mapping-form step went unseen: the consuming stage looked like it
    consumed nothing and the dependency edge vanished from show/graph."""
    flow = analyze_dataflow(
        *_scenario(
            {
                "producer": {"request": {"url": "http://server/a", "method": "POST"}, "response": {"grab": {"save": {"jmespath": {"token": "t"}}}}},
                "consumer": {"request": {"url": "http://server/b"}, "response": {"check": {"verify": {"expressions": ["{{ token != '' }}"]}}}},
            }
        )
    )
    assert flow.stages[0].saves == ["token"]
    assert flow.stages[1].consumes == ["token"]
    assert _edges(flow) == [{"producer": 0, "consumer": 1, "vars": ["token"]}]


def test_mapping_form_response_step_ordering_matches_list_form():
    """A mapping whose value is a list flattens in order, so raw step K still
    pairs with the validated response[K] — the re-save shadowing rule that
    depends on step order keeps working."""
    flow = analyze_dataflow(
        *_scenario(
            {
                "producer": {"request": {"url": "http://server/a"}, "response": [{"save": {"jmespath": {"token": "t"}}}]},
                "consumer": {
                    "request": {"url": "http://server/b"},
                    "response": {"steps": [{"save": {"jmespath": {"token": "t2"}}}, {"verify": {"expressions": ["{{ token != '' }}"]}}]},
                },
            }
        )
    )
    # The re-save precedes the reference, so the consumer reads its own value.
    assert flow.stages[1].consumes == []
