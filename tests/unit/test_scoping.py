"""Unit tests for the shared scope model (pytest_httpchain.scoping).

The scope rules are encoded twice — statically (`StageScopes`, name sets) and
at runtime (the context builders, value ChainMaps). These tests pin each view
and, crucially, assert the two views correspond on a concrete scenario.
"""

import pytest

from pytest_httpchain.models import Scenario
from pytest_httpchain.scoping import TEMPLATE_BUILTINS as SCOPING_BUILTINS
from pytest_httpchain.scoping import (
    DefinedNames,
    NameUnion,
    SkipCause,
    base_global_context,
    defined_names,
    extract_builtin_stand_ins,
    extract_invalid_expressions,
    extract_template_variables,
    extract_uncalled_builtins,
    iteration_context,
    marks_skip_cause,
    response_step_context,
    response_step_templates,
    saved_in_response,
    saved_in_stage,
    stage_scopes,
    stage_start_context,
    stats_name,
    with_saves,
    with_stage_substitutions,
)
from pytest_httpchain.templates import TEMPLATE_BUILTINS, TemplatesError, walk
from tests.unit.helpers import BEYOND_RECURSION_LIMIT, nested

NOTHING_DEFINED = DefinedNames(names=frozenset(), callables=frozenset())

# A scenario that saves `timestamp` and `len`, has a variable `rows`, a function
# substitution `fetch`, a fixture `sha256` and a function substitution `get`.
DEFINED = DefinedNames(
    names=frozenset({"timestamp", "len", "rows", "fetch", "sha256", "get"}),
    callables=frozenset({"fetch", "sha256", "get"}),
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

    def test_phases_are_the_properties_uncopied(self):
        """`phases` gives every phase at once as `NameUnion`s of the
        ingredients, the same names the properties give as frozensets (the
        validator's checks ask for all of them, stage by stage, and a
        frozenset union copied a scenario's thousands of vars each time)."""
        for scope in stage_scopes(make_scenario()):
            phases = scope.phases()
            assert (set(phases.always_run), set(phases.pre_iteration), set(phases.request), set(phases.response)) == (
                scope.always_run,
                scope.pre_iteration,
                scope.request,
                scope.response,
            )
            # Nothing skippable: the scope is its own when_skipped.
            assert scope.when_skipped is scope

    def test_name_union(self):
        """Membership asks each part; ``|`` adds one, uncopied; any other set
        operation gives a frozenset."""
        union = NameUnion(frozenset({"a", "b"}), frozenset({"b", "c"}))
        assert "c" in union
        assert "d" not in union
        assert sorted(union) == ["a", "b", "c"]
        assert len(union) == 3
        wider = union | {"d"}
        assert isinstance(wider, NameUnion)
        assert "d" in wider
        assert "d" not in union
        assert {"a", "x"} - union == {"x"}
        assert union - {"a"} == frozenset({"b", "c"})
        assert union & {"a", "z"} == frozenset({"a"})
        assert union == {"a", "b", "c"}

    def test_names_only_stages_that_may_skip_save_are_skippable(self):
        """A skipped stage saves nothing and the chain goes on, so a name only
        such stages save is in scope only when one of them ran. One a stage
        without skip_if (or with skip_if: false) saves too is always there."""

        def saving(name: str, *saved: str, **fields) -> dict:
            return {"name": name, "request": {"url": "http://server/"}, "response": [{"save": {"jmespath": dict.fromkeys(saved, "a")}}], **fields}

        scopes = stage_scopes(
            Scenario.model_validate(
                {
                    "stages": [
                        saving("maybe", "token", "both", skip_if="{{ flag }}"),
                        saving("never", "kept", skip_if=False),
                        saving("sure", "both"),
                        saving("skipped", "gone", skip_if=True),
                        saving("last"),
                    ]
                }
            )
        )
        assert [scope.skippable_saves for scope in scopes] == [set(), {"token", "both"}, {"token", "both"}, {"token"}, {"token", "gone"}]
        last = scopes[-1]
        assert last.earlier_saves == {"token", "both", "kept", "gone"}
        assert last.when_skipped.earlier_saves == {"both", "kept"}
        assert last.request - last.when_skipped.request == {"token", "gone"}
        assert last.when_skipped.skippable_saves == set()

    def test_a_stage_mark_that_may_skip_makes_its_saves_skippable(self):
        """A skipped or xfailed stage is reported skipped, which leaves the
        chain going, just as a skip_if skip does."""

        def saving(name: str, marks: list[str]) -> dict:
            return {"name": name, "marks": marks, "request": {"url": "http://server/"}, "response": [{"save": {"jmespath": {"token": "a"}}}]}

        scopes = stage_scopes(
            Scenario.model_validate(
                {
                    "stages": [
                        saving("marked", ["skip"]),
                        saving("expected_failure", ["xfail"]),
                        saving("inactive", ["skipif(False, reason='never')"]),
                        saving("last", []),
                    ]
                }
            )
        )
        assert [scope.skippable_saves for scope in scopes] == [set(), {"token"}, {"token"}, set()]
        assert [scope.skip_cause for scope in scopes] == [
            SkipCause(mark="skip", never_called=True),
            SkipCause(mark="xfail", fails_as_expected=True),
            None,
            None,
        ]

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            # One pytest never calls first: it reads nothing.
            pytest.param({"skip_if": "{{ flag }}", "marks": ["xfail", "skip"]}, SkipCause(mark="skip", never_called=True), id="never-called-first"),
            pytest.param({"skip_if": "{{ flag }}", "marks": ["xfail"]}, SkipCause(mark=None), id="then-skip-if"),
            # pytest looks at skip and skipif at setup, before any xfail.
            pytest.param({"marks": ["slow", "xfail", "skipif('sys.version_info < (3, 99)')"]}, SkipCause(mark="skipif('sys.version_info < (3, 99)')"), id="then-skip-or-skipif"),
            pytest.param({"marks": ["slow", "xfail"]}, SkipCause(mark="xfail", fails_as_expected=True), id="then-xfail"),
        ],
    )
    def test_skip_cause_of_a_stage_with_several(self, fields, expected):
        stage = {"name": "s", "request": {"url": "http://server/"}, **fields}
        assert stage_scopes(Scenario.model_validate({"stages": [stage]}))[0].skip_cause == expected

    @pytest.mark.parametrize(
        ("stage_marks", "scenario_marks", "never_called"),
        [
            pytest.param(["skip"], [], True, id="own-skip"),
            pytest.param(["xfail(run=False)"], [], True, id="own-xfail-not-run"),
            pytest.param(["xfail"], [], False, id="own-xfail-runs"),
            # The scenario's marks apply to every stage: here they count.
            pytest.param([], ["skip"], True, id="scenario-skip"),
            pytest.param([], ["xfail(run=False)"], True, id="scenario-xfail-not-run"),
            # The stage's own xfail comes first, and decides.
            pytest.param(["xfail"], ["xfail(run=False)"], False, id="own-xfail-before-scenario-xfail"),
        ],
    )
    def test_never_called_reads_the_stage_and_scenario_marks(self, stage_marks, scenario_marks, never_called):
        stage = {"name": "s", "marks": stage_marks, "request": {"url": "http://server/"}}
        assert stage_scopes(Scenario.model_validate({"marks": scenario_marks, "stages": [stage]}))[0].never_called is never_called

    def test_scenario_marks_are_no_skip_cause(self):
        """They apply to the stage that reads a save as much as to the one that
        saves it, so they cannot skip one and not the other."""
        stage = {"name": "s", "request": {"url": "http://server/"}, "response": [{"save": {"jmespath": {"token": "a"}}}]}
        scopes = stage_scopes(Scenario.model_validate({"marks": ["skip"], "stages": [stage, {**stage, "name": "t"}]}))
        assert [(scope.skip_cause, scope.skippable_saves) for scope in scopes] == [(None, frozenset())] * 2

    def test_stats_as_is_a_save_no_phase_of_its_stage_sees(self):
        """A stage's stats exist once every iteration has ended: the stages
        after it read them, none of its own response steps does, even after
        a save whose names are unknown (a user function's), which makes every
        name the steps save visible."""
        scenario = Scenario.model_validate(
            {
                "stages": [
                    {
                        "name": "load",
                        "parallel": {"repeat": 2, "stats_as": "stats"},
                        "request": {"url": "http://server/"},
                        "response": [{"save": {"user_functions": ["mod:fn"]}}, {"save": {"jmespath": {"id": "a"}}}, {"verify": {"status": 200}}],
                    },
                    {"name": "after", "request": {"url": "http://server/"}},
                ]
            }
        )
        load, after = scenario.stages
        assert (saved_in_response(load), stats_name(load), saved_in_stage(load)) == ({"id"}, "stats", {"id", "stats"})
        assert (saved_in_response(after), stats_name(after), saved_in_stage(after)) == (set(), None, set())
        assert [prior for _, prior in response_step_templates(load, None)] == [set(), {"id"}, {"id"}]
        scopes = stage_scopes(scenario)
        assert scopes[0].saves == {"id", "stats"}
        assert "stats" not in scopes[0].response
        assert "stats" in scopes[1].request


class TestContextBuilders:
    def test_layering_order(self):
        """Each later layer shadows every earlier one: step saves over iteration
        params over stage substitutions over fixtures over saves over scenario vars."""
        context = base_global_context({"name": "scenario", "svar": 1})
        context = with_saves(context, {"name": "save"})
        assert context["name"] == "save"
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

    def test_response_namespace_shadows_user_variable(self):
        """`response` is layered last, so neither a user var nor an earlier
        step's save of that name can hide the HTTP metadata in response steps."""
        meta = object()
        iteration = iteration_context(base_global_context({"response": "scenario var"}), {})
        assert response_step_context(with_saves(iteration, {"response": "step save"}), meta)["response"] is meta

    def test_base_global_context_is_pristine_base(self):
        """Saves layer on top; maps[-1] stays the original scenario context,
        which teardown_class relies on to reset between reruns."""
        base = {"svar": 1}
        context = with_saves(with_saves(base_global_context(base), {"a": 1}), {"b": 2})
        assert context.maps[-1] == base


@pytest.mark.parametrize(
    ("mark", "expected"),
    [
        pytest.param("skip", SkipCause(mark="skip", never_called=True), id="skip"),
        pytest.param("skip(reason='wip')", SkipCause(mark="skip(reason='wip')", never_called=True), id="skip-with-reason"),
        pytest.param("skipif(True, reason='off')", SkipCause(mark="skipif(True, reason='off')", never_called=True), id="skipif-true"),
        # No condition at all is unconditional, as in pytest's skipping plugin.
        pytest.param("skipif(reason='off')", SkipCause(mark="skipif(reason='off')", never_called=True), id="skipif-no-condition"),
        # pytest applies the mark when ANY condition is true.
        pytest.param("skipif(False, 1, reason='off')", SkipCause(mark="skipif(False, 1, reason='off')", never_called=True), id="skipif-any-true"),
        # A string condition is an expression pytest evaluates at run time...
        pytest.param("skipif('sys.version_info < (3, 99)')", SkipCause(mark="skipif('sys.version_info < (3, 99)')"), id="skipif-string"),
        # ... unless it is a literal, which evaluates the same here.
        pytest.param("skipif('True', reason='off')", SkipCause(mark="skipif('True', reason='off')", never_called=True), id="skipif-literal-string"),
        pytest.param("skipif('False', reason='on')", None, id="skipif-false-literal-string"),
        pytest.param("skipif(False, reason='on')", None, id="skipif-false"),
        pytest.param("skipif(condition=0, reason='on')", None, id="skipif-falsy-kwarg"),
        # An xfail runs the stage, and an expected failure discards its saves.
        pytest.param("xfail", SkipCause(mark="xfail", fails_as_expected=True), id="xfail"),
        pytest.param("xfail('sys.version_info < (3, 99)')", SkipCause(mark="xfail('sys.version_info < (3, 99)')", fails_as_expected=True), id="xfail-string"),
        pytest.param("xfail(run=False)", SkipCause(mark="xfail(run=False)", never_called=True), id="xfail-not-run"),
        pytest.param("xfail(condition=False, reason='on')", None, id="xfail-false"),
        # pytest fails the stage for a non-string condition without reason=,
        # true or not, and a failure aborts the chain.
        pytest.param("skipif(True)", None, id="skipif-bool-without-reason"),
        pytest.param("xfail(condition=True)", None, id="xfail-bool-without-reason"),
        pytest.param("skipif('True')", SkipCause(mark="skipif('True')", never_called=True), id="skipif-string-needs-no-reason"),
        # A mark can give raises= only a literal, never an exception type, so
        # pytest reports every failure as a real one.
        pytest.param("xfail(raises='ValueError')", None, id="xfail-raises"),
        pytest.param("xfail(run=False, raises='ValueError')", SkipCause(mark="xfail(run=False, raises='ValueError')", never_called=True), id="xfail-raises-not-run"),
        pytest.param("slow", None, id="unrelated"),
        # A mark that does not parse is HTTPCHAIN019's to report.
        pytest.param("skip(", None, id="unparseable"),
    ],
)
def test_marks_skip_cause(mark, expected):
    """Which stage marks leave a stage reported skipped, the chain going and
    its saves missing, read as pytest's skipping plugin reads them."""
    assert marks_skip_cause([mark]) == expected


@pytest.mark.parametrize(
    ("marks", "expected"),
    [
        # Of the xfail marks, the first that applies decides whether pytest runs the stage.
        pytest.param(["xfail(reason='a')", "xfail(run=False, reason='b')"], SkipCause(mark="xfail(reason='a')", fails_as_expected=True), id="first-xfail-decides"),
        pytest.param(
            ["xfail(False, reason='a')", "xfail(run=False, reason='b')"], SkipCause(mark="xfail(run=False, reason='b')", never_called=True), id="inapplicable-xfail-passed-over"
        ),
        # One that may apply comes first, so pytest may run the stage.
        pytest.param(
            ["xfail('sys.version_info < (3, 99)')", "xfail(run=False)"],
            SkipCause(mark="xfail('sys.version_info < (3, 99)')", fails_as_expected=True),
            id="xfail-that-may-apply-first",
        ),
        pytest.param(["xfail(raises='ValueError')", "xfail"], None, id="deciding-xfail-never-fails-as-expected"),
        # skip and skipif are looked at before any xfail.
        pytest.param(["xfail", "skip"], SkipCause(mark="skip", never_called=True), id="skip-before-xfail"),
    ],
)
def test_marks_skip_cause_reads_marks_in_pytests_order(marks, expected):
    assert marks_skip_cause(marks) == expected


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        pytest.param("{{ [y for y in items] }}", {"items"}, id="comprehension-targets-are-local"),
        pytest.param("{{ [x for x in items] + [x] }}", {"items", "x"}, id="target-does-not-hide-read-outside-comprehension"),
        pytest.param("{{ [x for x in x] }}", {"x"}, id="target-does-not-hide-its-own-iterable"),
        pytest.param("{{ [y for x in items for y in x] }}", {"items"}, id="earlier-target-is-visible-to-later-generator"),
        pytest.param("{{ [f() for f in items] }}", {"f", "items"}, id="call-uses-function-even-when-target-has-same-name"),
        # Text the engine refuses to evaluate fails wherever it renders, which
        # HTTPCHAIN037 reports; it names nothing. A regex fallback read every
        # identifier in it, and had `True` and `status_code` reported undefined.
        pytest.param("{{ items[ }}", set(), id="unparseable-names-nothing"),
        pytest.param("{{ response.status_code == True and ok) }}", set(), id="unparseable-names-no-keyword-or-attribute"),
        pytest.param("{{ ok = items }}", set(), id="assignment-names-nothing"),
        # The engine evaluates no lambda: its parameters, and `items`, are
        # never looked up.
        pytest.param("{{ sorted(items, key=lambda row: row.score) }}", set(), id="lambda-names-nothing"),
        # A list literal spreads a `*` itself, where the engine evaluates it.
        pytest.param("{{ [*items, *more] }}", {"items", "more"}, id="list-spread-names-its-operands"),
        # One statement, as the engine parses it: `x;` is `x`.
        pytest.param("{{ items; }}", {"items"}, id="trailing-semicolon-is-one-expression"),
        pytest.param("{{ len(items) }}", {"items"}, id="builtins-are-not-references"),
        # An escaped template is text, never evaluated, so it names nothing,
        # nor does a template's syntax in the text it escapes (up to its
        # `}}`); after a doubled backslash, or after that `}}`, one is read.
        pytest.param(r"\{{ items }}", set(), id="escaped-names-nothing"),
        pytest.param(r"\\{{ items }}", {"items"}, id="doubled-backslash-then-template"),
        pytest.param(r"\{{{{ items }}", set(), id="braces-inside-escaped-text-name-nothing"),
        pytest.param(r"\{{ a {{ items }} }}", set(), id="template-inside-escaped-text-names-nothing"),
        pytest.param(r"\{{ a }}{{ items }}", {"items"}, id="template-after-escaped-text"),
    ],
)
def test_template_references(template, expected):
    """Which names a `{{ }}` expression actually references. Undefined-variable
    and forward-reference diagnostics and the data-flow graph are all built on
    this set: a name wrongly included is a false warning on working scenarios, a
    name wrongly dropped is a missed one."""
    assert extract_template_variables(template, defined=NOTHING_DEFINED) == expected


def test_comprehension_target_scope_matches_runtime():
    context = {"items": [1], "x": 2, "f": lambda: 3}
    assert walk("{{ [x for x in items] + [x] }}", context) == [1, 2]
    assert walk("{{ [x for x in items] }}", context) == [1]
    assert walk("{{ [f() for f in items] }}", context) == [3]


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        # The scenario saves `timestamp`: the runtime looks a read up among the
        # user's names first, so it is the save.
        pytest.param("{{ timestamp }}", {"timestamp"}, id="read-of-a-user-name-is-a-reference"),
        pytest.param("{{ sorted(rows, key=len) }}", {"rows", "len"}, id="builtin-passed-as-a-value-is-read"),
        # A call falls back to the built-in when no user function of the name
        # is in scope, and a saved value is none: it consumes nothing.
        pytest.param("{{ timestamp() }}", set(), id="call-under-a-user-value-reaches-the-builtin"),
        pytest.param("{{ str(timestamp()) + timestamp }}", {"timestamp"}, id="read-and-call-is-read"),
        pytest.param("{{ now() }}", set(), id="undefined-builtin-call"),
        # Reported as an uncalled helper (HTTPCHAIN035), not as a reference.
        pytest.param("{{ now }}", set(), id="read-of-a-builtin-the-scenario-leaves-alone"),
        pytest.param("{{ fetch() }}", {"fetch"}, id="call-of-a-user-function-is-a-reference"),
        # A fixture may hold a function, which a call reaches first; out of the
        # fixture's scope the built-in runs instead, which is no failure, so it
        # is no reference (see extract_builtin_stand_ins).
        pytest.param("{{ sha256(body) }}", {"body"}, id="call-of-a-builtin-named-fixture-is-no-reference"),
        pytest.param("{{ [len for len in rows] }}", {"rows"}, id="comprehension-target-is-local-builtin-name-too"),
        pytest.param("{{ [len for len in rows] + [len] }}", {"rows", "len"}, id="builtin-named-target-does-not-hide-outside-read"),
    ],
)
def test_template_references_to_builtin_names(template, expected):
    """A built-in's name counts as a reference only where the scenario defines
    that name itself and a template reads it. Dropping it always — as before
    the time and hashing helpers took common names — lost a save named
    `timestamp` from `show`/`graph`, and let a bare `{{ timestamp }}` ahead of
    that save render the built-in function into the request unreported."""
    assert extract_template_variables(template, defined=DEFINED) == expected


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        # `sha256` is a fixture: out of its scope, a call runs the built-in.
        pytest.param("{{ sha256(body) }}", {"sha256"}, id="call-of-a-builtin-named-fixture"),
        # A saved value is no function: the call always reaches the built-in.
        pytest.param("{{ timestamp() }}", set(), id="call-under-a-builtin-named-save"),
        pytest.param("{{ now() }}", set(), id="builtin-the-scenario-leaves-alone"),
        pytest.param("{{ fetch() }}", set(), id="not-a-builtin-name"),
        # Handed to a function that takes one, the name is read, and a value
        # the scenario saves under it would be what the key gets where in
        # scope; out of scope the built-in stands in, and works.
        pytest.param("{{ sorted(rows, key=len) }}", {"len"}, id="builtin-named-save-passed-as-key"),
        pytest.param("{{ sign(sha256) }}", {"sha256"}, id="builtin-named-fixture-passed-to-a-user-function"),
        pytest.param("{{ fetch().ids(sha256) }}", {"sha256"}, id="builtin-named-fixture-passed-to-a-method-of-a-users-object"),
        # A save is data: out of its scope the user's function gets the
        # built-in function in the value's place, and fails on it. The save is
        # missing there, which the reference checks report.
        pytest.param("{{ sign(timestamp) }}", set(), id="builtin-named-save-passed-to-a-user-function"),
        pytest.param("{{ fetch().ids(timestamp) }}", set(), id="builtin-named-save-passed-to-a-method-of-a-users-object"),
        pytest.param("{{ sorted(rows, key=timestamp) + sign(timestamp) }}", set(), id="builtin-named-save-passed-as-key-and-to-a-user-function"),
        # A method of data takes no function: the argument is a value.
        pytest.param("{{ rows.index(sha256) }}", set(), id="builtin-named-fixture-passed-to-a-method-of-data"),
        # Also used as a value: out of scope that use gets the built-in
        # function, which the reference checks report.
        pytest.param("{{ sha256(body) + str(sha256) }}", set(), id="also-a-value"),
        # get()/exists() are merged over the user's callables: always the built-in.
        pytest.param("{{ get('rows', []) }}", set(), id="get-is-never-shadowed"),
    ],
)
def test_builtin_stand_ins(template, expected):
    """The built-ins' names a template uses only as functions where the
    scenario defines them too: out of the scope of that definition the built-in
    stands in silently, which the validator reports as such (HTTPCHAIN036)
    rather than as an undefined name, since nothing fails."""
    assert extract_builtin_stand_ins(template, defined=DEFINED) == expected


def test_runtime_resolves_builtin_names_as_the_reference_model_says():
    """The reference model's premises, on the evaluator itself: a user value
    shadows a read but not a call, a user function shadows a call, and a user
    function named get never does."""
    context = {"timestamp": "saved", "sha256": lambda _: "user sha256", "get": lambda *_: "user get", "rows": [1]}
    assert walk("{{ timestamp }}", context) == "saved"
    assert isinstance(walk("{{ timestamp() }}", context), int)
    assert walk("{{ sha256('x') }}", context) == "user sha256"
    assert walk("{{ get('rows') }}", context) == [1]


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        pytest.param("{{ now }}", {"now"}, id="whole-template"),
        pytest.param("sent {{ timestamp_ms }}", {"timestamp_ms"}, id="interpolated"),
        pytest.param("{{ 'at ' + now }}", {"now"}, id="operand"),
        # No built-in or method takes a function positionally: str() of one is its repr.
        pytest.param("{{ str(timestamp_ms) }}", {"timestamp_ms"}, id="argument-of-a-builtin"),
        pytest.param("{{ ', '.join(now) }}", {"now"}, id="argument-of-a-method"),
        # A key= or a user function's argument may take one.
        pytest.param("{{ sorted(rows, key=sha256) }}", set(), id="key-of-a-builtin"),
        pytest.param("{{ rows.sort(key=sha256) }}", set(), id="key-of-a-method"),
        pytest.param("{{ sign(now) }}", set(), id="argument-of-a-user-function"),
        pytest.param("{{ sign(clock=now) }}", set(), id="keyword-argument-of-a-user-function"),
        # A method of an object reached from a fixture or function
        # substitution may take one too; one of data, a save or a variable, not.
        pytest.param("{{ fetch().ids(uuid4) }}", set(), id="argument-of-a-method-of-a-users-object"),
        pytest.param("{{ sha256.api.ids(n=2, gen=uuid4) }}", set(), id="keyword-argument-of-a-method-of-a-users-object"),
        pytest.param("{{ rows.index(now) }}", {"now"}, id="argument-of-a-method-of-data"),
        pytest.param("{{ [row.get(now) for row in rows] }}", {"now"}, id="argument-of-a-method-of-a-comprehension-target"),
        # Any other keyword of a built-in or a method is a value: dict() is how
        # a mapping is built for urlencode() without the `}}` gotcha.
        pytest.param("{{ urlencode(dict(sort=timestamp_ms)) }}", {"timestamp_ms"}, id="keyword-argument-of-a-builtin"),
        pytest.param("{{ 'a,b'.split(sep=now) }}", {"now"}, id="keyword-argument-of-a-method"),
        pytest.param("{{ now() }}", set(), id="called"),
        # The older built-ins of no use as a value count too. env's repr was
        # os.environ's, every variable with its value.
        pytest.param("X-Env: {{ env }}", {"env"}, id="env"),
        pytest.param("{{ [uuid4, rand, randint] }}", {"uuid4", "rand", "randint"}, id="uuid4-rand-randint"),
        # Not call-only: `key=len` is how len is used as a value.
        pytest.param("{{ len }}", set(), id="not-call-only"),
        # The scenario's own name: a reference, checked as one.
        pytest.param("{{ timestamp }}", set(), id="user-name"),
        pytest.param("{{ [now for now in rows] }}", set(), id="comprehension-target"),
        pytest.param("{{ [now for now in rows] + [now] }}", {"now"}, id="target-does-not-hide-uncalled-builtin-outside-comprehension"),
        pytest.param("{{ now( }}", set(), id="unparseable"),
    ],
)
def test_uncalled_builtins(template, expected):
    """The call-only built-ins a template uses without calling them
    (HTTPCHAIN035): none of them is any use as a value, so `{{ now }}` is
    `{{ now() }}` with the parentheses forgotten."""
    assert extract_uncalled_builtins(template, defined=DEFINED) == expected


def test_defined_names_cover_every_definition():
    """What a built-in's name is looked up in: every kind of name a scenario
    introduces, at scenario and stage level, and the fixtures as the names that
    may hold a function."""
    assert defined_names(make_scenario()) == DefinedNames(
        names=frozenset({"sfix", "svar", "f1", "p1", "sub1", "item", "saved1", "saved2"}),
        callables=frozenset({"sfix", "f1"}),
    )


def test_defined_callables_include_every_function_substitution():
    scenario = Scenario.model_validate(
        {
            "substitutions": [{"functions": {"scenario_fn": "m:f"}}],
            "stages": [
                {
                    "name": "s",
                    "substitutions": [{"vars": {"value": 1}}, {"functions": {"stage_fn": "m:f"}}],
                    "request": {"url": "http://server/a"},
                    "response": [{"save": {"substitutions": [{"functions": {"save_fn": "m:f"}}]}}],
                }
            ],
        }
    )
    assert defined_names(scenario).callables == {"scenario_fn", "stage_fn", "save_fn"}


def test_invalid_expressions_are_listed_once_each_in_document_order():
    """What HTTPCHAIN037 reports: each template the engine refuses to
    evaluate, as its message writes it, with the engine's reason; a valid one,
    whatever `=` or `;` it holds in a keyword argument or a string, is none."""
    fragment = {
        "headers": {"X-A": "{{ a = 1 }}", "X-Ok": "{{ dict(k=v) }}-{{ ';' }}"},
        "params": ["{{ 1 + }}", "x {{a = 1}} y", {"q": "{{ b; c }}"}],
    }
    assert extract_invalid_expressions(fragment) == {
        "{{ a = 1 }}": "a template holds one expression, not an assignment; to compare two values, write '=='",
        "{{ 1 + }}": "invalid syntax",
        "{{ b; c }}": "a template holds one expression, not 2 statements separated by ';'",
    }


@pytest.mark.parametrize(
    "template",
    [
        "{{ a = 1 }}",
        "{{ 1 + }}",
        "{{ b; c }}",
        "x {{ }} y",
        "{{ sorted(a, key=lambda r: r) }}",
        # Beside an escaped one, which is neither reported nor rendered.
        r"\{{ a = 1 }} {{ b; c }}",
        # A lone surrogate (one JSON \u escape away) is no text to parse; each
        # message escapes it, where printing it crashed `validate`.
        "{{ 'a\ud800' }}",
    ],
)
def test_every_invalid_expression_fails_to_render(template):
    """The static and the runtime view agree: what the validator reports as
    invalid is what the engine refuses, with the same reason."""
    [(written, reason)] = extract_invalid_expressions(template).items()
    with pytest.raises(TemplatesError) as excinfo:
        walk(template, {"a": 1, "b": 1, "c": 1})
    assert str(excinfo.value) == f"Invalid expression '{written}': {reason}"
    # Printable on any UTF-8 stream.
    str(excinfo.value).encode("utf-8")


def test_template_references_found_at_any_depth():
    """Iterative: a recursive walk spent two frames per level of nesting, and
    crashed `validate`, collection, `show` and `graph` on a value a few hundred
    levels deep that loads fine."""
    assert extract_template_variables(nested("{{ token }}", BEYOND_RECURSION_LIMIT), defined=NOTHING_DEFINED) == {"token"}
    assert extract_uncalled_builtins(nested("{{ now }}", BEYOND_RECURSION_LIMIT), defined=NOTHING_DEFINED) == {"now"}
    assert extract_builtin_stand_ins(nested("{{ now() }}", BEYOND_RECURSION_LIMIT), defined=DefinedNames(names=frozenset({"now"}), callables=frozenset({"now"}))) == {"now"}
    assert list(extract_invalid_expressions(nested("{{ 1 + }}", BEYOND_RECURSION_LIMIT))) == ["{{ 1 + }}"]


def test_template_builtins_is_single_source():
    """M14: the reference extractor uses the canonical TEMPLATE_BUILTINS from the
    templates package, not a private copy that could drift from the engine."""
    assert SCOPING_BUILTINS is TEMPLATE_BUILTINS
