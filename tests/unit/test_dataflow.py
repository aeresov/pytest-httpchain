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
    diags = check_scenario(sc, data)
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
    diags = check_scenario(sc, data)
    assert DiagnosticCode.FIXTURE_CONFLICT in _codes(diags)


def test_m13_scenario_substitution_undefined_is_error():
    # M13: a scenario-level substitution referencing an undefined name is a
    # guaranteed collection-time crash, reported as HTTPCHAIN017 (error).
    data = {
        "substitutions": [{"vars": {"a": "{{ missing }}"}}],
        "stages": [{"name": "s", "request": {"url": "https://x.test/"}, "response": [{"verify": {"status": 200}}]}],
    }
    sc = Scenario.model_validate(data)
    diags = check_scenario(sc, data)
    assert any(d.code == DiagnosticCode.SCENARIO_UNDEFINED_VAR and d.severity == "error" for d in diags), [d.message for d in diags]


def test_m13_scenario_substitution_self_reference_ok():
    # An earlier scenario substitution referenced by a later one is in scope.
    data = {
        "substitutions": [{"vars": {"base": "https://x.test"}}, {"vars": {"url": "{{ base }}/a"}}],
        "stages": [{"name": "s", "request": {"url": "{{ url }}"}, "response": [{"verify": {"status": 200}}]}],
    }
    sc = Scenario.model_validate(data)
    diags = check_scenario(sc, data)
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
    diags = check_scenario(sc, data)
    undefined = [d for d in diags if d.code == DiagnosticCode.UNDEFINED_VAR]
    # The phase is the whole explanation here — `wid` IS defined a few lines
    # below, just not yet when substitutions resolve. Saying only "stage 's'"
    # sends the author looking for a typo that isn't there.
    assert [(d.location, d.message) for d in undefined] == [("stages[0].substitutions", "Stage 's': substitutions references potentially undefined variable(s): ['wid']")], (
        undefined
    )


def test_undefined_names_are_reported_per_phase():
    """Two phases referencing different undefined names are two findings, each
    pointing at its own phase — not one bag naming the stage."""
    sc, data = _scenario(
        [
            {
                "name": "s",
                "request": {"url": "https://x.test/{{ nope_req }}"},
                "response": [{"verify": {"status": 200, "expressions": ["{{ nope_resp }}"]}}],
            },
        ]
    )
    diags = check_scenario(sc, data)
    undefined = {d.location: d.message for d in diags if d.code == DiagnosticCode.UNDEFINED_VAR}

    assert set(undefined) == {"stages[0].request", "stages[0].response"}, undefined
    assert "nope_req" in undefined["stages[0].request"]
    assert "nope_resp" in undefined["stages[0].response"]


def test_dataflow_locations_are_indexed_json_paths():
    """`Diagnostic.location` is documented as a machine-routable address, so an
    unnamed stage must still produce a usable one (it used to be "")."""
    sc, data = _scenario([{"request": {"url": "https://x.test/{{ nope }}"}, "response": [{"verify": {"status": 200}}]}])
    diags = check_scenario(sc, data)

    assert all(d.location for d in diags), [d for d in diags if not d.location]
    assert {d.location for d in diags if d.code == DiagnosticCode.UNDEFINED_VAR} == {"stages[0].request"}


def test_saved_response_name_not_consumed_in_response_steps():
    """The reserved `response` metadata namespace shadows a same-named earlier
    save inside response steps, so a `response` reference there is not a data
    dependency; in a request template it still is."""
    data = {
        "stages": [
            {
                "name": "producer",
                "request": {"url": "http://server/a"},
                "response": [{"save": {"jmespath": {"response": "body.data"}}}],
            },
            {
                "name": "verify_meta",
                "request": {"url": "http://server/b"},
                "response": [{"verify": {"expressions": ["{{ response.status == 200 }}"]}}],
            },
            {
                "name": "request_consumer",
                "request": {"url": "http://server/c", "headers": {"x-d": "{{ response }}"}},
                "response": [{"verify": {"status": 200}}],
            },
        ]
    }
    flow = analyze_dataflow(Scenario.model_validate(data), data)

    assert flow.stages[1].consumes == []  # metadata namespace, not the save
    assert flow.stages[2].consumes == ["response"]  # request scope: real dependency


def test_foreach_param_does_not_shadow_substitution_phase_refs():
    """Stage substitutions resolve BEFORE iterations exist (stage_start_context),
    so a foreach parameter cannot shadow an earlier save referenced there — the
    producer edge is real and must be drawn."""
    sc, data = _scenario(
        [
            {"name": "a", "request": {"url": "https://x.test/", "method": "POST"}, "response": [{"save": {"jmespath": {"x": "id"}}}]},
            {
                "name": "b",
                "substitutions": [{"vars": {"y": "{{ x }}"}}],
                "parallel": {"foreach": [{"individual": {"x": [1, 2]}}]},
                "request": {"url": "https://x.test/{{ x }}/{{ y }}"},
                "response": [{"verify": {"status": 200}}],
            },
        ]
    )
    flow = analyze_dataflow(sc, data)
    assert flow.stages[1].consumes == ["x"]
    assert [e.model_dump() for e in flow.edges] == [{"producer": 0, "consumer": 1, "vars": ["x"]}]


def test_foreach_param_does_not_shadow_parallel_config_refs():
    """The parallel config resolves before any iteration exists, so its own
    foreach parameter names cannot shadow an earlier save referenced by e.g.
    max_concurrency."""
    sc, data = _scenario(
        [
            {"name": "a", "request": {"url": "https://x.test/", "method": "POST"}, "response": [{"save": {"jmespath": {"n": "id"}}}]},
            {
                "name": "b",
                "parallel": {"foreach": [{"individual": {"n": [1, 2]}}], "max_concurrency": "{{ n }}"},
                "request": {"url": "https://x.test/"},
                "response": [{"verify": {"status": 200}}],
            },
        ]
    )
    flow = analyze_dataflow(sc, data)
    assert flow.stages[1].consumes == ["n"]


def test_prior_substitution_step_shadows_substitution_phase_ref():
    """A prior step's name shadows the earlier save for later steps — no edge."""
    sc, data = _scenario(
        [
            {"name": "a", "request": {"url": "https://x.test/", "method": "POST"}, "response": [{"save": {"jmespath": {"t": "id"}}}]},
            {
                "name": "b",
                "substitutions": [{"vars": {"t": "local"}}, {"vars": {"u": "{{ t }}"}}],
                "request": {"url": "https://x.test/"},
                "response": [{"verify": {"status": 200}}],
            },
        ]
    )
    flow = analyze_dataflow(sc, data)
    assert flow.stages[1].consumes == []
    assert flow.edges == []


def test_later_substitution_step_does_not_shadow_earlier_step_ref():
    """Substitution steps resolve in order: a ref in step 1 to a name step 2
    defines reads the EARLIER SAVE at runtime, so the edge must be drawn."""
    sc, data = _scenario(
        [
            {"name": "a", "request": {"url": "https://x.test/", "method": "POST"}, "response": [{"save": {"jmespath": {"t": "id"}}}]},
            {
                "name": "b",
                "substitutions": [{"vars": {"u": "{{ t }}"}}, {"vars": {"t": "local"}}],
                "request": {"url": "https://x.test/"},
                "response": [{"verify": {"status": 200}}],
            },
        ]
    )
    flow = analyze_dataflow(sc, data)
    assert flow.stages[1].consumes == ["t"]
    assert [e.model_dump() for e in flow.edges] == [{"producer": 0, "consumer": 1, "vars": ["t"]}]


def test_scenario_substitution_forward_ref_is_error():
    """A scenario-level entry referencing a name only a LATER entry defines
    validates the way it runs: steps resolve strictly in order, so this is a
    guaranteed crash at scenario initialization (M-review false negative)."""
    data = {
        "substitutions": [{"vars": {"a": "{{ b }}"}}, {"vars": {"b": "hello"}}],
        "stages": [{"name": "s", "request": {"url": "https://x.test/"}, "response": [{"verify": {"status": 200}}]}],
    }
    sc = Scenario.model_validate(data)
    diags = check_scenario(sc, data)
    assert any(d.code == DiagnosticCode.SCENARIO_UNDEFINED_VAR and d.severity == "error" and "before the substitution step" in d.message for d in diags), [d.message for d in diags]


def test_scenario_substitution_same_entry_ref_is_error():
    """The runtime computes each entry's context before that entry's own vars
    land, so a same-entry reference crashes exactly like a forward one."""
    data = {
        "substitutions": [{"vars": {"a": "x", "b": "{{ a }}"}}],
        "stages": [{"name": "s", "request": {"url": "https://x.test/"}, "response": [{"verify": {"status": 200}}]}],
    }
    sc = Scenario.model_validate(data)
    diags = check_scenario(sc, data)
    assert any(d.code == DiagnosticCode.SCENARIO_UNDEFINED_VAR and d.severity == "error" for d in diags), [d.message for d in diags]


def test_scenario_functions_kwargs_are_dead_text_no_error():
    """`functions` kwargs are passed to wrap_function raw — never rendered — so
    template-looking text inside them must not produce a collection-blocking
    error (M-review false positive; the stage-level check already knew this)."""
    data = {
        "substitutions": [{"functions": {"tok": {"name": "somemod:make_token", "kwargs": {"payload": "literal {{ not_a_var }} text"}}}}],
        "stages": [{"name": "s", "request": {"url": "https://x.test/"}, "response": [{"verify": {"status": 200}}]}],
    }
    sc = Scenario.model_validate(data)
    diags = check_scenario(sc, data)
    assert DiagnosticCode.SCENARIO_UNDEFINED_VAR not in _codes(diags), [d.message for d in diags]


def test_scenario_functions_templated_name_is_checked():
    """Templated function import names ARE rendered at seed time, so their
    references participate in the order-aware check: a prior-step name is fine,
    an unknown one is the same guaranteed init crash as in a vars value."""
    ok = {
        "substitutions": [{"vars": {"mod": "x"}}, {"functions": {"f": "{{ mod }}:fn"}}],
        "stages": [{"name": "s", "request": {"url": "https://x.test/"}, "response": [{"verify": {"status": 200}}]}],
    }
    diags = check_scenario(Scenario.model_validate(ok), ok)
    assert DiagnosticCode.SCENARIO_UNDEFINED_VAR not in _codes(diags), [d.message for d in diags]

    bad = {
        "substitutions": [{"functions": {"f": "{{ missing_mod }}:fn"}}],
        "stages": [{"name": "s", "request": {"url": "https://x.test/"}, "response": [{"verify": {"status": 200}}]}],
    }
    diags = check_scenario(Scenario.model_validate(bad), bad)
    assert DiagnosticCode.SCENARIO_UNDEFINED_VAR in _codes(diags), [d.message for d in diags]


def test_reserved_marker_name_is_diagnostic_not_crash():
    """pytest's MarkGenerator raises AttributeError for underscore-prefixed
    names; the validator must report INVALID_MARKER, not blow up (M-review:
    only ValueError/SyntaxError were caught)."""
    data = {
        "stages": [{"name": "s", "marks": ["_foo"], "request": {"url": "https://x.test/"}, "response": [{"verify": {"status": 200}}]}],
    }
    sc = Scenario.model_validate(data)
    diags = check_scenario(sc, data)
    assert any(d.code == DiagnosticCode.INVALID_MARKER and d.severity == "error" for d in diags), [d.message for d in diags]


def test_resave_shadows_own_reference_in_later_response_steps():
    """Once a stage re-saves a name, later response steps read the stage's own
    fresh value (per-step with_saves layering), so no dependency edge on the
    earlier saver exists; a reference BEFORE the re-save still depends on it."""
    base_stage = {
        "name": "producer",
        "request": {"url": "http://server/a"},
        "response": [{"save": {"jmespath": {"token": "t"}}}],
    }

    resave_then_ref = {
        "stages": [
            base_stage,
            {
                "name": "consumer",
                "request": {"url": "http://server/b"},
                "response": [
                    {"save": {"jmespath": {"token": "t2"}}},
                    {"verify": {"expressions": ["{{ token != '' }}"]}},
                ],
            },
        ]
    }
    flow = analyze_dataflow(Scenario.model_validate(resave_then_ref), resave_then_ref)
    assert flow.stages[1].consumes == [], flow.edges

    ref_then_resave = {
        "stages": [
            base_stage,
            {
                "name": "consumer",
                "request": {"url": "http://server/b"},
                "response": [
                    {"verify": {"expressions": ["{{ token != '' }}"]}},
                    {"save": {"jmespath": {"token": "t2"}}},
                ],
            },
        ]
    }
    flow = analyze_dataflow(Scenario.model_validate(ref_then_resave), ref_then_resave)
    assert flow.stages[1].consumes == ["token"]
    assert any(e.producer == 0 and e.consumer == 1 and e.vars == ["token"] for e in flow.edges), flow.edges


def test_mapping_form_response_steps_are_analyzed():
    """The name-keyed `response` mapping form is first-class. Re-deriving the
    raw shape with a bare isinstance(list) discarded it, so every reference
    inside a mapping-form step went unseen: the consuming stage looked like it
    consumed nothing and the dependency edge vanished from show/graph."""
    data = {
        "stages": {
            "producer": {
                "request": {"url": "http://server/a", "method": "POST"},
                "response": {"grab": {"save": {"jmespath": {"token": "t"}}}},
            },
            "consumer": {
                "request": {"url": "http://server/b"},
                "response": {"check": {"verify": {"expressions": ["{{ token != '' }}"]}}},
            },
        }
    }
    flow = analyze_dataflow(Scenario.model_validate(data), data)

    assert flow.stages[0].saves == ["token"]
    assert flow.stages[1].consumes == ["token"]
    assert [e.model_dump() for e in flow.edges] == [{"producer": 0, "consumer": 1, "vars": ["token"]}]


def test_mapping_form_response_step_ordering_matches_list_form():
    """A mapping whose value is a list flattens in order, so raw step K still
    pairs with the validated response[K] — the re-save shadowing rule that
    depends on step order keeps working."""
    data = {
        "stages": {
            "producer": {"request": {"url": "http://server/a"}, "response": [{"save": {"jmespath": {"token": "t"}}}]},
            "consumer": {
                "request": {"url": "http://server/b"},
                "response": {"steps": [{"save": {"jmespath": {"token": "t2"}}}, {"verify": {"expressions": ["{{ token != '' }}"]}}]},
            },
        }
    }
    flow = analyze_dataflow(Scenario.model_validate(data), data)
    # The re-save precedes the reference, so the consumer reads its own value.
    assert flow.stages[1].consumes == []
