"""Unit tests for analyze_dataflow: which earlier saves each stage consumes,
and the producer -> consumer edges drawn from them."""

import pytest

from pytest_httpchain.dataflow import analyze_dataflow
from pytest_httpchain.models import Scenario


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
        pytest.param({"substitutions": [{"vars": {"x": "local"}}], "request": {"url": "https://x.test/{{ x }}"}}, [], id="stage-substitution-shadows"),
        # always_run resolves before stage substitutions exist, so it reads the
        # earlier save even when a stage substitution reuses the name.
        pytest.param({"substitutions": [{"vars": {"x": "local"}}], "always_run": "{{ x }}"}, ["x"], id="always-run"),
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
        # Substitution steps resolve in order: a PRIOR step's name shadows the
        # save for later steps, a LATER step's does not.
        pytest.param({"substitutions": [{"vars": {"x": "local"}}, {"vars": {"z": "{{ x }}"}}]}, [], id="prior-substitution-step-shadows"),
        pytest.param({"substitutions": [{"vars": {"z": "{{ x }}"}}, {"vars": {"x": "local"}}]}, ["x"], id="later-substitution-step"),
        # Response steps resolve in order with per-step with_saves layering:
        # once the stage re-saves a name, later steps read its own fresh value.
        pytest.param({"response": [{"save": {"jmespath": {"x": "b"}}}, {"verify": {"expressions": ["{{ x != '' }}"]}}]}, [], id="own-resave-shadows-later-steps"),
        pytest.param({"response": [{"verify": {"expressions": ["{{ x != '' }}"]}}, {"save": {"jmespath": {"x": "b"}}}]}, ["x"], id="reference-before-own-resave"),
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
