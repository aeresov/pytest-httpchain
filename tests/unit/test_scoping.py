"""Unit tests for the shared scope model (pytest_httpchain.scoping).

The scope rules are encoded twice — statically (`StageScopes`, name sets) and
at runtime (the context builders, value ChainMaps). These tests pin each view
and, crucially, assert the two views correspond on a concrete scenario.
"""

from pytest_httpchain.models import Scenario
from pytest_httpchain.scoping import (
    base_global_context,
    extract_template_variables,
    iteration_context,
    response_step_context,
    stage_scopes,
    stage_start_context,
    with_saves,
    with_stage_substitutions,
)


def make_scenario() -> Scenario:
    return Scenario.model_validate(
        {
            "fixtures": ["sfix"],
            "substitutions": [{"vars": {"svar": 1}}],
            "stages": [
                {
                    "name": "first",
                    "fixtures": ["f1"],
                    "parametrize": [{"individual": {"p1": [1, 2]}}],
                    "substitutions": [{"vars": {"sub1": "x"}}],
                    "parallel": {"foreach": [{"individual": {"item": [1, 2]}}]},
                    "request": {"url": "http://server/a"},
                    "response": [{"save": {"jmespath": {"saved1": "a"}}}],
                },
                {
                    "name": "second",
                    "request": {"url": "http://server/b"},
                    "response": [{"save": {"jmespath": {"saved2": "b"}}}],
                },
            ],
        }
    )


class TestStageScopes:
    def test_first_stage_phases(self):
        scope = stage_scopes(make_scenario())[0]

        assert scope.earlier_saves == frozenset()
        assert scope.always_run == {"svar", "sfix", "f1", "p1"}
        assert scope.pre_iteration == scope.always_run | {"sub1"}
        assert scope.request == scope.pre_iteration | {"item"}
        # Response steps additionally see the reserved `response` metadata
        # namespace; this stage's own saves are NOT in scope wholesale — each
        # step sees only what strictly earlier steps saved.
        assert scope.response == scope.request | {"response"}
        assert "saved1" not in scope.response

    def test_saves_accumulate_into_later_stages(self):
        scopes = stage_scopes(make_scenario())

        assert scopes[1].earlier_saves == {"saved1"}
        assert "saved1" in scopes[1].always_run
        # A later stage's saves are never in an earlier stage's scopes.
        assert "saved2" not in scopes[0].response

    def test_empty_scenario(self):
        assert stage_scopes(Scenario.model_validate({"stages": []})) == []


class TestContextBuilders:
    def test_layering_order(self):
        """Each later layer shadows every earlier one: iteration params over
        stage substitutions over fixtures over saves over scenario vars."""
        context = base_global_context({"name": "scenario", "svar": 1})
        context = with_saves(context, {"name": "save"})
        context = stage_start_context(context, {"name": "fixture"})
        assert context["name"] == "fixture"
        context = with_stage_substitutions(context, {"name": "substitution"})
        assert context["name"] == "substitution"
        context = iteration_context(context, {"name": "iteration"})
        assert context["name"] == "iteration"
        context = with_saves(context, {"name": "step-save"})
        assert context["name"] == "step-save"
        assert context["svar"] == 1  # unshadowed names stay visible throughout

    def test_runtime_static_correspondence(self):
        """The names visible in the runtime contexts equal the static phase
        sets — the invariant that makes the validator trustworthy."""
        scenario = make_scenario()
        scopes = stage_scopes(scenario)

        global_context = base_global_context({"svar": 1})

        for scope in scopes:
            # pytest injects fixtures and parametrize parameters through the
            # generated method signature; the carrier collects them into one dict.
            stage_fixtures = dict.fromkeys(scope.scenario_fixtures | scope.stage_fixtures | scope.parametrize_params, "value")

            stage_start = stage_start_context(global_context, stage_fixtures)
            assert set(stage_start) == scope.always_run

            local = with_stage_substitutions(stage_start, dict.fromkeys(scope.stage_substitutions, "value"))
            assert set(local) == scope.pre_iteration

            iteration = iteration_context(local, dict.fromkeys(scope.foreach_params, "value"))
            assert set(iteration) == scope.request

            # The carrier rebuilds the step context per step, so `response`
            # sits above whatever earlier steps saved into the iteration context.
            responded = response_step_context(iteration, response_meta=object())
            assert set(responded) == scope.response
            after_saves = response_step_context(with_saves(iteration, dict.fromkeys(scope.saves, "value")), response_meta=object())
            assert set(after_saves) == scope.response | scope.saves

            global_context = with_saves(global_context, dict.fromkeys(scope.saves, "value"))

    def test_base_global_context_is_pristine_base(self):
        """Saves layer on top; maps[-1] stays the original scenario context,
        which teardown_class relies on to reset between reruns."""
        base = {"svar": 1}
        context = with_saves(with_saves(base_global_context(base), {"a": 1}), {"b": 2})
        assert context.maps[-1] == base


class TestReferenceExtraction:
    """Which names a `{{ }}` expression actually references.

    Everything downstream — undefined-variable and forward-reference
    diagnostics, the data-flow graph — is built on this set, so a name wrongly
    included is a false warning on working scenarios and a name wrongly dropped
    is a missed one.
    """

    def test_comprehension_targets_are_local(self):
        assert extract_template_variables("{{ [y for y in items] }}") == {"items"}

    def test_lambda_parameters_are_local(self):
        """A lambda parameter is bound by the lambda, not supplied by the
        context — `key=lambda row: ...` must not demand a `row` variable."""
        assert extract_template_variables("{{ sorted(items, key=lambda row: row.score) }}") == {"items"}

    def test_every_lambda_parameter_kind_is_bound(self):
        """Positional-only, positional, *args, keyword-only and **kwargs alike:
        walking only `args.args` would leave the other four looking undefined."""
        assert extract_template_variables("{{ (lambda p, /, a, *rest, b=1, **kw: [p, a, rest, b, kw])(1, 2) }}") == set()

    def test_unparseable_expression_falls_back_to_identifiers(self):
        """A malformed expression still fails at runtime, but the validator
        must not go blind: the regex fallback keeps reporting the names it can
        see rather than silently claiming the expression references nothing."""
        assert extract_template_variables("{{ items[ }}") == {"items"}

    def test_builtins_are_not_context_references(self):
        assert extract_template_variables("{{ len(items) }}") == {"items"}
