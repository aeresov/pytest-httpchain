"""Unit tests for analyze_dataflow."""

from pytest_httpchain_models import Scenario

from pytest_httpchain.dataflow import analyze_dataflow


def _scenario(stages):
    data = {"stages": stages}
    return Scenario.model_validate(data), data


def test_consume_edge_from_earlier_stage():
    sc, data = _scenario(
        [
            {"name": "create", "request": {"url": "https://x.test/u", "method": "POST"}, "response": [{"save": {"jmespath": {"user_id": "id"}}}]},
            {"name": "get", "request": {"url": "https://x.test/u/{{ user_id }}"}, "response": [{"verify": {"status": 200}}]},
        ]
    )
    flow = analyze_dataflow(sc, data)
    assert flow.stages[0].saves == ["user_id"]
    assert flow.stages[1].consumes == ["user_id"]
    assert [e.model_dump() for e in flow.edges] == [{"producer": 0, "consumer": 1, "vars": ["user_id"]}]


def test_local_redefinition_not_consumed():
    sc, data = _scenario(
        [
            {"name": "a", "request": {"url": "https://x.test/", "method": "POST"}, "response": [{"save": {"jmespath": {"token": "t"}}}]},
            {"name": "b", "substitutions": [{"vars": {"token": "override"}}], "request": {"url": "https://x.test/{{ token }}"}, "response": [{"verify": {"status": 200}}]},
        ]
    )
    flow = analyze_dataflow(sc, data)
    assert flow.stages[1].consumes == []
    assert flow.edges == []


def test_multiple_vars_same_pair_merged():
    sc, data = _scenario(
        [
            {"name": "a", "request": {"url": "https://x.test/", "method": "POST"}, "response": [{"save": {"jmespath": {"id": "id", "token": "t"}}}]},
            {"name": "b", "request": {"url": "https://x.test/{{ id }}", "headers": {"Authorization": "{{ token }}"}}, "response": [{"verify": {"status": 200}}]},
        ]
    )
    flow = analyze_dataflow(sc, data)
    assert len(flow.edges) == 1
    assert flow.edges[0].vars == ["id", "token"]


def test_no_edges_when_independent():
    sc, data = _scenario(
        [
            {"name": "a", "request": {"url": "https://x.test/a"}, "response": [{"verify": {"status": 200}}]},
            {"name": "b", "request": {"url": "https://x.test/b"}, "response": [{"verify": {"status": 200}}]},
        ]
    )
    flow = analyze_dataflow(sc, data)
    assert flow.edges == []
    assert flow.stages[1].method == "GET"


def test_same_stage_save_and_use_no_self_edge():
    sc, data = _scenario(
        [
            {
                "name": "a",
                "request": {"url": "https://x.test/", "method": "POST"},
                "response": [
                    {"save": {"jmespath": {"token": "t"}}},
                    {"verify": {"expressions": ["{{ token != '' }}"]}},
                ],
            },
        ]
    )
    flow = analyze_dataflow(sc, data)
    assert flow.stages[0].saves == ["token"]
    assert flow.stages[0].consumes == []
    assert flow.edges == []


def test_parametrize_ref_not_consumed():
    sc, data = _scenario(
        [
            {"name": "a", "request": {"url": "https://x.test/", "method": "POST"}, "response": [{"save": {"jmespath": {"page": "p"}}}]},
            {
                "name": "b",
                "parametrize": [{"individual": {"n": ["{{ page }}"]}}],
                "request": {"url": "https://x.test/{{ n }}"},
                "response": [{"verify": {"status": 200}}],
            },
        ]
    )
    flow = analyze_dataflow(sc, data)
    assert "page" not in flow.stages[1].consumes
    assert flow.edges == []


def test_earliest_producer_selected():
    sc, data = _scenario(
        [
            {"name": "a", "request": {"url": "https://x.test/a", "method": "POST"}, "response": [{"save": {"jmespath": {"x": "v"}}}]},
            {"name": "b", "request": {"url": "https://x.test/b", "method": "POST"}, "response": [{"save": {"jmespath": {"x": "v"}}}]},
            {"name": "c", "request": {"url": "https://x.test/{{ x }}"}, "response": [{"verify": {"status": 200}}]},
        ]
    )
    flow = analyze_dataflow(sc, data)
    assert [(e.producer, e.consumer, e.vars) for e in flow.edges] == [(0, 2, ["x"])]
    assert flow.stages[2].consumes == ["x"]
