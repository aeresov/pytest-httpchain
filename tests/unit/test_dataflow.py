"""Unit tests for analyze_dataflow (and the closely related check_scenario flow)."""

from pytest_httpchain.dataflow import analyze_dataflow
from pytest_httpchain.models import Scenario
from pytest_httpchain.validation import DiagnosticCode, check_scenario


def _scenario(stages):
    data = {"stages": stages}
    return Scenario.model_validate(data), data


def _codes(diags):
    return {d.code for d in diags}


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


def test_scenario_fixture_shadows_save_not_consumed():
    # At runtime a scenario fixture shadows a same-named save in every stage,
    # so the reference resolves to the fixture — no producer->consumer edge.
    data = {
        "fixtures": ["token"],
        "stages": [
            {"name": "a", "request": {"url": "https://x.test/", "method": "POST"}, "response": [{"save": {"jmespath": {"token": "t"}}}]},
            {"name": "b", "request": {"url": "https://x.test/{{ token }}"}, "response": [{"verify": {"status": 200}}]},
        ],
    }
    sc = Scenario.model_validate(data)
    flow = analyze_dataflow(sc, data)
    assert flow.stages[1].consumes == []
    assert flow.edges == []
    assert flow.scenario_fixtures == ["token"]


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


def test_always_run_ref_shadowed_by_stage_substitution_still_consumed():
    # always_run resolves before stage substitutions exist, so the earlier save
    # IS read at runtime even when a stage substitution reuses the name.
    sc, data = _scenario(
        [
            {"name": "a", "request": {"url": "https://x.test/", "method": "POST"}, "response": [{"save": {"jmespath": {"token": "t"}}}]},
            {
                "name": "b",
                "substitutions": [{"vars": {"token": "stage-local"}}],
                "always_run": "{{ token }}",
                "request": {"url": "https://x.test/static"},
                "response": [{"verify": {"status": 200}}],
            },
        ]
    )
    flow = analyze_dataflow(sc, data)
    assert flow.stages[1].consumes == ["token"]
    assert [(e.producer, e.consumer, e.vars) for e in flow.edges] == [(0, 1, ["token"])]


def test_always_run_ref_consumed():
    # always_run resolves against earlier saves, so referencing one is a
    # genuine producer -> consumer dependency.
    sc, data = _scenario(
        [
            {"name": "create", "request": {"url": "https://x.test/", "method": "POST"}, "response": [{"save": {"jmespath": {"resource_id": "id"}}}]},
            {
                "name": "cleanup",
                "always_run": "{{ resource_id }}",
                "request": {"url": "https://x.test/static"},
                "response": [{"verify": {"status": 200}}],
            },
        ]
    )
    flow = analyze_dataflow(sc, data)
    assert flow.stages[1].consumes == ["resource_id"]
    assert [(e.producer, e.consumer, e.vars) for e in flow.edges] == [(0, 1, ["resource_id"])]


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


def test_latest_producer_selected():
    # M10: a re-saved variable is attributed to its LAST writer before the consumer
    # (stage b, index 1), matching runtime ChainMap layering — not the first (stage a).
    sc, data = _scenario(
        [
            {"name": "a", "request": {"url": "https://x.test/a", "method": "POST"}, "response": [{"save": {"jmespath": {"x": "v"}}}]},
            {"name": "b", "request": {"url": "https://x.test/b", "method": "POST"}, "response": [{"save": {"jmespath": {"x": "v"}}}]},
            {"name": "c", "request": {"url": "https://x.test/{{ x }}"}, "response": [{"verify": {"status": 200}}]},
        ]
    )
    flow = analyze_dataflow(sc, data)
    assert [(e.producer, e.consumer, e.vars) for e in flow.edges] == [(1, 2, ["x"])]
    assert flow.stages[2].consumes == ["x"]


def test_m12_cross_stage_fixture_and_param_not_conflict():
    # M12: a fixture used only in stage A and a same-named parametrize parameter
    # used only in stage B never coexist, so this must NOT be a conflict error.
    sc, data = _scenario(
        [
            {"name": "a", "fixtures": ["token"], "request": {"url": "https://x.test/a"}, "response": [{"verify": {"status": 200}}]},
            {"name": "b", "parametrize": [{"individual": {"token": [1, 2]}}], "request": {"url": "https://x.test/{{ token }}"}, "response": [{"verify": {"status": 200}}]},
        ]
    )
    diags, _ = check_scenario(sc, data)
    assert DiagnosticCode.FIXTURE_CONFLICT not in _codes(diags)


def test_m12_same_stage_fixture_and_var_conflict():
    # M12: a fixture and a same-named substitution variable IN THE SAME stage still conflict.
    sc, data = _scenario(
        [
            {
                "name": "a",
                "fixtures": ["token"],
                "substitutions": [{"vars": {"token": "x"}}],
                "request": {"url": "https://x.test/a"},
                "response": [{"verify": {"status": 200}}],
            },
        ]
    )
    diags, _ = check_scenario(sc, data)
    assert DiagnosticCode.FIXTURE_CONFLICT in _codes(diags)


def test_m13_scenario_substitution_undefined_is_error():
    # M13: a scenario-level substitution referencing an undefined name is a
    # guaranteed collection-time crash, reported as HTTPCHAIN017 (error).
    data = {
        "substitutions": [{"vars": {"a": "{{ missing }}"}}],
        "stages": [{"name": "s", "request": {"url": "https://x.test/"}, "response": [{"verify": {"status": 200}}]}],
    }
    sc = Scenario.model_validate(data)
    diags, _ = check_scenario(sc, data)
    assert any(d.code == DiagnosticCode.SCENARIO_UNDEFINED_VAR and d.severity == "error" for d in diags), [d.message for d in diags]


def test_m13_scenario_substitution_self_reference_ok():
    # An earlier scenario substitution referenced by a later one is in scope.
    data = {
        "substitutions": [{"vars": {"base": "https://x.test"}}, {"vars": {"url": "{{ base }}/a"}}],
        "stages": [{"name": "s", "request": {"url": "{{ url }}"}, "response": [{"verify": {"status": 200}}]}],
    }
    sc = Scenario.model_validate(data)
    diags, _ = check_scenario(sc, data)
    assert DiagnosticCode.SCENARIO_UNDEFINED_VAR not in _codes(diags)


def test_m11_substitution_referencing_foreach_param_is_flagged():
    # M11: stage substitutions resolve before any foreach iteration variable exists,
    # so referencing a foreach parameter in a substitution is undefined — even though
    # the request (resolved per iteration) may reference it fine.
    sc, data = _scenario(
        [
            {
                "name": "s",
                "substitutions": [{"vars": {"derived": "{{ wid }}-x"}}],
                "parallel": {"foreach": [{"individual": {"wid": [1, 2]}}]},
                "request": {"url": "https://x.test/{{ wid }}"},
                "response": [{"verify": {"status": 200}}],
            },
        ]
    )
    diags, _ = check_scenario(sc, data)
    undefined_msgs = [d.message for d in diags if d.code == DiagnosticCode.UNDEFINED_VAR]
    assert any("wid" in m for m in undefined_msgs), undefined_msgs
