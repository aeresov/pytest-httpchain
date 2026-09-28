"""Unit tests for `validation.check_scenario` (validation/semantic.py) on
in-memory scenarios. The file-based, end-to-end validator tests live in
test_validation.py."""

import pytest

from pytest_httpchain.models import Scenario
from pytest_httpchain.validation import DiagnosticCode, check_scenario
from tests.unit.helpers import BEYOND_RECURSION_LIMIT, nested

C = DiagnosticCode
_STAGE = {"name": "s", "request": {"url": "https://x.test/"}, "response": [{"verify": {"status": 200}}]}


def _check(stages, **top):
    data = {"stages": stages, **top}
    return check_scenario(Scenario.model_validate(data), data)


@pytest.mark.parametrize(
    ("stages", "conflict"),
    [
        # A fixture used only in stage A and a same-named parametrize parameter
        # used only in stage B never coexist (M12).
        pytest.param(
            [
                {**_STAGE, "name": "a", "fixtures": ["token"]},
                {**_STAGE, "name": "b", "parametrize": [{"individual": {"token": [1, 2]}}], "request": {"url": "https://x.test/{{ token }}"}},
            ],
            False,
            id="across-stages",
        ),
        pytest.param([{**_STAGE, "fixtures": ["token"], "substitutions": [{"vars": {"token": "x"}}]}], True, id="same-stage"),
    ],
)
def test_fixture_var_conflict_is_scoped_per_stage(stages, conflict):
    codes = {d.code for d in _check(stages)}
    assert (DiagnosticCode.FIXTURE_CONFLICT in codes) is conflict


@pytest.mark.parametrize(
    ("substitutions", "expected"),
    [
        # Scenario-level substitutions resolve before any stage runs, so an
        # unavailable name is a guaranteed crash at initialization (M13).
        pytest.param([{"vars": {"a": "{{ missing }}"}}], "Undefined variable(s) in scenario-level 'substitutions' templates: ['missing']", id="undefined"),
        pytest.param([{"vars": {"base": "https://x.test"}}, {"vars": {"url": "{{ base }}/a"}}], None, id="prior-entry"),
        # Entries resolve strictly in order, each before its own vars land, so
        # a forward or same-entry reference crashes like an undefined one.
        pytest.param([{"vars": {"a": "{{ b }}"}}, {"vars": {"b": "hello"}}], "before the substitution step that defines them: ['b']", id="forward-entry"),
        pytest.param([{"vars": {"a": "x", "b": "{{ a }}"}}], "before the substitution step that defines them: ['a']", id="same-entry"),
        # `functions` kwargs are passed to wrap_function raw — never rendered.
        pytest.param([{"functions": {"tok": {"name": "somemod:make_token", "kwargs": {"payload": "literal {{ not_a_var }} text"}}}}], None, id="function-kwargs"),
        # Templated function import names ARE rendered at seed time.
        pytest.param([{"vars": {"mod": "x"}}, {"functions": {"f": "{{ mod }}:fn"}}], None, id="function-name-prior-entry"),
        pytest.param([{"functions": {"f": "{{ missing_mod }}:fn"}}], "templates: ['missing_mod']", id="function-name-undefined"),
        # The built-in helpers are there before any substitution step.
        pytest.param([{"vars": {"started": "{{ now() }}", "day": "{{ now('%Y-%m-%d') }}", "sig": "{{ sha256(str(timestamp())) }}"}}], None, id="builtin-helpers"),
    ],
)
def test_scenario_substitution_references(substitutions, expected):
    found = [d for d in _check([_STAGE], substitutions=substitutions) if d.code == DiagnosticCode.SCENARIO_UNDEFINED_VAR]
    if expected is None:
        assert found == []
    else:
        assert [(d.severity, d.location) for d in found] == [("error", "substitutions")], found
        assert expected in found[0].message


NOW_STANDS_IN = "uses the function now(), but the scenario's own definition of 'now'"


@pytest.mark.parametrize(
    ("stages", "top", "message"),
    [
        # A scenario-level template sees no fixture and nothing a stage defines:
        # the call runs the built-in, where a READ of the fixture crashes (016).
        pytest.param([_STAGE], {"fixtures": ["now"]}, NOW_STANDS_IN, id="scenario-fixture"),
        pytest.param([{**_STAGE, "fixtures": ["now"]}], {}, NOW_STANDS_IN, id="stage-fixture"),
        pytest.param([{**_STAGE, "substitutions": [{"functions": {"now": "clock:fixed"}}]}], {}, NOW_STANDS_IN, id="stage-function"),
        # Names that were built-ins before the helpers came: this validated
        # clean, then became a collection error, though it runs.
        pytest.param(
            [{**_STAGE, "fixtures": ["str"], "substitutions": [{"functions": {"len": "builtins:len"}}]}],
            {},
            "uses the functions len(), str(), but the scenario's own definitions of ['len', 'str'] are not in scope there",
            id="older-builtins",
        ),
    ],
)
def test_scenario_level_call_of_a_builtin_named_definition_is_a_warning(stages, top, message):
    """A call under a built-in's name the scenario defines only where no
    scenario-level template can see it never fails: the built-in runs. So it is
    a warning (HTTPCHAIN036), never the 016/017 errors that stopped collection
    of a scenario that runs."""
    substitutions = [{"vars": {"started": "{{ now() }}", "size": "{{ len([1, 2]) }}", "label": "{{ str(3) }}"}}]
    diags = _check(stages, substitutions=substitutions, **top)
    assert [(d.code, d.severity, d.location) for d in diags] == [(C.BUILTIN_STANDS_IN, "warning", "substitutions")]
    assert diags[0].message.startswith(f"Scenario-level 'substitutions' {message}"), diags[0].message


@pytest.mark.parametrize(
    ("substitutions", "code", "message"),
    [
        # A read of a fixture crashes at scenario initialization: the built-in
        # function is no value ("Uncalled function").
        pytest.param(
            [{"vars": {"started": "{{ now }}"}}],
            C.FIXTURE_IN_SCENARIO_TEMPLATE,
            "Fixtures referenced in scenario-level 'substitutions' templates: ['now'] (the scenario-level context never includes fixture values; "
            "where no definition of 'now' is in scope, the name is the template built-in function, not a value, and a template that renders to it "
            "crashes scenario initialization)",
            id="read-of-a-fixture",
        ),
        # So does a read ahead of the step defining it.
        pytest.param(
            [{"vars": {"started": "{{ now }}"}}, {"vars": {"now": "x"}}],
            C.SCENARIO_UNDEFINED_VAR,
            "Scenario-level 'substitutions' references name(s) before the substitution step that defines them: ['now'] "
            "(steps resolve strictly in order; where no definition of 'now' is in scope, the name is the template built-in function, not a value, "
            "and a template that renders to it crashes scenario initialization)",
            id="forward-read-of-a-call-only-builtin-name",
        ),
        # A read of any other built-in's name gets the built-in function as
        # its value, and nothing crashes: no crash note.
        pytest.param(
            [{"vars": {"top": "{{ max }}"}}, {"vars": {"max": 3}}],
            C.SCENARIO_UNDEFINED_VAR,
            "Scenario-level 'substitutions' references name(s) before the substitution step that defines them: ['max'] "
            "(steps resolve strictly in order; where no definition of 'max' is in scope, the template built-in of that name is used instead)",
            id="forward-read-of-a-builtin-name",
        ),
        pytest.param(
            [{"vars": {"top": "{{ b }}"}}, {"vars": {"b": 3}}],
            C.SCENARIO_UNDEFINED_VAR,
            "Scenario-level 'substitutions' references name(s) before the substitution step that defines them: ['b'] "
            "(steps resolve strictly in order; this crashes at scenario initialization)",
            id="forward-read",
        ),
        # A call ahead of the step defining the function runs the built-in.
        pytest.param(
            [{"vars": {"started": "{{ now() }}"}}, {"functions": {"now": "clock:fixed"}}],
            C.BUILTIN_STANDS_IN,
            "Scenario-level 'substitutions' uses the function now(), but the scenario's own definition of 'now' is not in scope there "
            "(a scenario-level substitution step sees only the steps before it: no fixture, and nothing a stage defines), so the template built-in runs instead",
            id="forward-call",
        ),
    ],
)
def test_scenario_level_builtin_names_read_and_called(substitutions, code, message):
    """The scenario requests a `now` fixture, or later defines a `now`
    function, which no scenario-level template sees."""
    fixtures = [] if any("now" in {**step.get("vars", {}), **step.get("functions", {})} for step in substitutions) else ["now"]
    diags = _check([_STAGE], substitutions=substitutions, fixtures=fixtures)
    assert [(d.code, d.location, d.message) for d in diags] == [(code, "substitutions", message)]


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        # Handed to sorted(), the built-in len stands in for the save.
        pytest.param("{{ sorted(names, key=len) }}", [(C.BUILTIN_STANDS_IN, "substitutions")], id="passed-as-a-function"),
        # As a value, it is the built-in function: undefined, as any read.
        pytest.param("{{ len }}", [(C.SCENARIO_UNDEFINED_VAR, "substitutions")], id="read-as-a-value"),
    ],
)
def test_scenario_level_builtin_name_passed_as_a_function(template, expected):
    """A save named `len` is never in scope at scenario level. A template that
    hands `len` to a function there gets the built-in, which works, so it is
    the HTTPCHAIN036 warning; one using it as a value gets the function
    itself, the 017 error."""
    saves_len = {**_STAGE, "response": [{"verify": {"status": 200}}, {"save": {"jmespath": {"len": "size"}}}]}
    diags = _check([saves_len], substitutions=[{"vars": {"names": ["bb", "a"]}}, {"vars": {"x": template}}])
    assert [(d.code, d.location) for d in diags] == expected


def test_reads_of_several_builtin_names_name_every_builtin_used_instead():
    """Called ``max`` and ``min`` are used as the value; a call-only built-in
    (`test_reads_of_call_only_and_other_builtin_names_get_a_note_each`) is not."""
    saves = {**_STAGE, "response": [{"verify": {"status": 200}}, {"save": {"jmespath": {"max": "hi", "min": "lo"}}}]}
    diags = _check([saves], substitutions=[{"vars": {"bounds": "{{ [min, max] }}"}}])
    assert [(d.code, d.message) for d in diags] == [
        (
            C.SCENARIO_UNDEFINED_VAR,
            "Undefined variable(s) in scenario-level 'substitutions' templates: ['max', 'min'] (resolved against only scenario substitutions, before any "
            "stage runs; where no definition of ['max', 'min'] is in scope, the template built-ins of those names are used instead)",
        )
    ]


def test_reads_of_call_only_and_other_builtin_names_get_a_note_each():
    """A built-in used as a value and a call-only one, which a template must
    not render to, read in one template: each note says what that read gets."""
    saves = {**_STAGE, "response": [{"verify": {"status": 200}}, {"save": {"jmespath": {"max": "hi", "now": "at", "env": "e"}}}]}
    diags = _check([saves], substitutions=[{"vars": {"bounds": "{{ [max, now, env] }}"}}])
    assert [(d.code, d.message) for d in diags] == [
        (
            C.SCENARIO_UNDEFINED_VAR,
            "Undefined variable(s) in scenario-level 'substitutions' templates: ['env', 'max', 'now'] (resolved against only scenario substitutions, "
            "before any stage runs; where no definition of 'max' is in scope, the template built-in of that name is used instead; where no definition "
            "of ['env', 'now'] is in scope, the names are the template built-in functions, not values, and a template that renders to one crashes "
            "scenario initialization)",
        )
    ]


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        # Parametrize values resolve against scenario substitutions only.
        pytest.param(
            {"parametrize": [{"individual": {"n": ["{{ [timestamp(), 1] }}"]}}]},
            (
                C.BUILTIN_STANDS_IN,
                "stages[0].parametrize",
                "Stage 's': parametrize value uses the function timestamp(), but the scenario's own definition of 'timestamp' is not in scope "
                "there (values resolve against scenario-level substitutions only), so the template built-in runs instead",
            ),
            id="parametrize-call",
        ),
        pytest.param(
            {"parametrize": [{"individual": {"n": ["{{ timestamp }}"]}}]},
            (
                C.UNDEFINED_VAR,
                "stages[0].parametrize",
                "Stage 's': parametrize value references 'timestamp' — only scenario-level substitutions are in scope when values are resolved; "
                "where no definition of 'timestamp' is in scope, the name is the template built-in function, not a value, and a template that "
                "renders to it fails the scenario's collection",
            ),
            id="parametrize-read",
        ),
        pytest.param(
            {"always_run": "{{ timestamp() > 0 }}"},
            (
                C.BUILTIN_STANDS_IN,
                "stages[0].always_run",
                "Stage 's': always_run uses the function timestamp(), but the scenario's own definition of 'timestamp' is not in scope there, "
                "so the template built-in runs instead",
            ),
            id="always-run-call",
        ),
        pytest.param(
            {"always_run": "{{ timestamp != 0 }}"},
            (
                C.UNDEFINED_VAR,
                "stages[0].always_run",
                "Stage 's': always_run references 'timestamp' — potentially not in scope; only fixtures, parametrize parameters, scenario substitutions, "
                "and variables saved by earlier stages are available; where no definition of 'timestamp' is in scope, the name is the template built-in "
                "function, not a value, and a template that renders to it fails the stage",
            ),
            id="always-run-read",
        ),
    ],
)
def test_builtin_named_definition_out_of_scope_in_parametrize_and_always_run(stage, expected):
    """A later stage's `timestamp` fixture is out of scope in this one: a call
    runs the built-in (HTTPCHAIN036), a read gets the built-in function, and
    every message says which built-in stands in."""
    diags = _check([{**_STAGE, **stage}, {**_STAGE, "name": "clock", "fixtures": ["timestamp"]}])
    assert [(d.code, d.location, d.message) for d in diags if d.code != C.PARAMETRIZE_COLLECTION_RESOLUTION] == [expected]


@pytest.mark.parametrize(
    "stage",
    [
        # A functions substitution's kwargs reach the function raw (HTTPCHAIN030).
        pytest.param({"substitutions": [{"functions": {"f": {"name": "os:getcwd", "kwargs": {"x": "{{ now }}"}}}}]}, id="function-kwargs"),
        # process_save renders a substitutions save's entries, nothing else.
        pytest.param(
            {"response": [{"save": {"description": "{{ now }}", "substitutions": [{"vars": {"a": 1}}]}}, {"verify": {"status": 200}}]},
            id="substitutions-save-description",
        ),
    ],
)
def test_uncalled_helper_in_text_never_rendered_is_not_reported(stage):
    """HTTPCHAIN035 reads what the runtime renders, as the reference checks do:
    a helper in dead text cannot fail the stage it says it fails."""
    assert C.UNCALLED_BUILTIN not in {d.code for d in _check([{**_STAGE, **stage}])}


@pytest.mark.parametrize(
    ("top", "stage", "location", "message"),
    [
        pytest.param(
            {},
            {"request": {"url": "https://x.test/", "headers": {"X-Env": "e={{ env }}"}}},
            "stages[0].request",
            "Stage 's': request uses the built-in function 'env' without calling it: the template gets the function itself, not its value, "
            "and one that renders to a function fails the stage. Write env(...)",
            id="stage",
        ),
        # retry renders with the parallel config, before any request.
        pytest.param(
            {},
            {"retry": {"attempts": 3, "delay": "{{ rand }}"}},
            "stages[0].retry",
            "Stage 's': retry uses the built-in function 'rand' without calling it: the template gets the function itself, not its value, "
            "and one that renders to a function fails the stage. Write rand()",
            id="retry",
        ),
        # Parametrize values render at collection, scenario-level templates at
        # scenario initialization: a refusal there fails that.
        pytest.param(
            {},
            {"parametrize": [{"individual": {"n": ["{{ uuid4 }}"]}}]},
            "stages[0].parametrize",
            "Stage 's': parametrize uses the built-in function 'uuid4' without calling it: the template gets the function itself, not its value, "
            "and one that renders to a function fails the scenario's collection. Write uuid4()",
            id="parametrize",
        ),
        pytest.param(
            {"substitutions": [{"vars": {"started": "{{ now }}"}}]},
            {},
            "substitutions",
            "Scenario-level 'substitutions' uses the built-in function 'now' without calling it: the template gets the function itself, not its "
            "value, and one that renders to a function crashes scenario initialization. Write now()",
            id="scenario-level",
        ),
    ],
)
def test_uncalled_builtin_says_what_fails(top, stage, location, message):
    diags = [d for d in _check([{**_STAGE, **stage}], **top) if d.code == C.UNCALLED_BUILTIN]
    assert [(d.location, d.message) for d in diags] == [(location, message)]


def test_substitution_referencing_foreach_param_is_flagged():
    """Stage substitutions resolve before any foreach iteration variable exists,
    so referencing a foreach parameter there is undefined — even though the
    request (resolved per iteration) may reference it fine (M11). The phase is
    the whole explanation: `wid` IS defined a few lines below, just not yet when
    substitutions resolve, so the message must name the phase."""
    diags = _check(
        [
            {
                **_STAGE,
                "substitutions": [{"vars": {"derived": "{{ wid }}-x"}}],
                "parallel": {"foreach": [{"individual": {"wid": [1, 2]}}]},
                "request": {"url": "https://x.test/{{ wid }}"},
            }
        ]
    )
    undefined = [(d.location, d.message) for d in diags if d.code == DiagnosticCode.UNDEFINED_VAR]
    assert undefined == [("stages[0].substitutions", "Stage 's': substitutions references potentially undefined variable(s): ['wid']")]


def test_undefined_names_are_reported_per_phase():
    """Two phases referencing different undefined names are two findings, each
    pointing at its own phase — not one bag naming the stage."""
    diags = _check([{**_STAGE, "request": {"url": "https://x.test/{{ nope_req }}"}, "response": [{"verify": {"status": 200, "expressions": ["{{ nope_resp }}"]}}]}])
    undefined = {d.location: d.message for d in diags if d.code == DiagnosticCode.UNDEFINED_VAR}
    assert set(undefined) == {"stages[0].request", "stages[0].response"}, undefined
    assert "nope_req" in undefined["stages[0].request"]
    assert "nope_resp" in undefined["stages[0].response"]


def _saving(name: str, *saved: str, **fields) -> dict:
    return {**_STAGE, "name": name, "response": [{"save": {"jmespath": dict.fromkeys(saved, "a")}}, {"verify": {"status": 200}}], **fields}


READ_TOKEN = "https://x.test/{{ token }}"


@pytest.mark.parametrize(
    ("reader", "location"),
    [
        pytest.param({"request": {"url": READ_TOKEN}}, "stages[1].request", id="request"),
        pytest.param({"always_run": "{{ token != '' }}"}, "stages[1].always_run", id="always-run"),
        pytest.param({"substitutions": [{"vars": {"t": "{{ token }}"}}]}, "stages[1].substitutions", id="substitutions"),
        pytest.param({"skip_if": "{{ token == '' }}"}, "stages[1].skip_if", id="skip-if"),
        pytest.param({"parallel": {"repeat": 2, "max_concurrency": "{{ token }}"}}, "stages[1].parallel", id="parallel"),
        pytest.param({"retry": {"attempts": 2, "delay": "{{ token }}"}}, "stages[1].retry", id="retry"),
        pytest.param({"response": [{"verify": {"expressions": ["{{ token != '' }}"]}}]}, "stages[1].response", id="response"),
    ],
)
def test_name_only_a_stage_that_may_skip_saves_is_potentially_undefined(reader, location):
    """In every phase of every later stage: a skipped stage, unlike a failed
    one, does not stop the stages after it."""
    diags = _check([_saving("login", "token", skip_if="{{ flag }}"), {**_STAGE, "name": "use", **reader}], substitutions=[{"vars": {"flag": True}}])
    phase = location.rsplit(".", 1)[1]
    assert [(d.code, d.location, d.message) for d in diags if d.code != C.NO_VERIFY] == [
        (
            C.UNDEFINED_VAR,
            location,
            f"Stage 'use': {phase} references 'token', which only stage 'login' saves, and it has skip_if: when it skips, 'token' is undefined here "
            f"— read it with get('token', <default>)",
        )
    ]


@pytest.mark.parametrize(
    ("stages", "flagged"),
    [
        # Absent only when both skipped.
        pytest.param(
            [_saving("a", "token", skip_if="{{ flag }}"), _saving("b", "token", skip_if=True), {**_STAGE, "request": {"url": READ_TOKEN}}],
            "which only stages 'a', 'b' save, and each has skip_if: when they all skip, 'token' is undefined here",
            id="several-stages-that-may-skip",
        ),
        # A stage that never skips saves it too, before or after the one that may.
        pytest.param([_saving("a", "token"), _saving("b", "token", skip_if="{{ flag }}"), {**_STAGE, "request": {"url": READ_TOKEN}}], None, id="saved-for-sure-before"),
        pytest.param([_saving("a", "token", skip_if="{{ flag }}"), _saving("b", "token"), {**_STAGE, "request": {"url": READ_TOKEN}}], None, id="saved-for-sure-after"),
        pytest.param([_saving("a", "token", skip_if=False), {**_STAGE, "request": {"url": READ_TOKEN}}], None, id="skip-if-false"),
        # Where something else of the name is in scope, it is read instead.
        pytest.param(
            [_saving("a", "token", skip_if="{{ flag }}"), {**_STAGE, "substitutions": [{"vars": {"token": "anon"}}], "request": {"url": READ_TOKEN}}],
            None,
            id="shadowed-by-a-stage-substitution",
        ),
        pytest.param([_saving("a", "token", skip_if="{{ flag }}"), {**_STAGE, "fixtures": ["token"], "request": {"url": READ_TOKEN}}], None, id="shadowed-by-a-fixture"),
        pytest.param(
            [_saving("a", "token", skip_if="{{ flag }}"), {**_STAGE, "response": [{"save": {"jmespath": {"token": "t"}}}, {"verify": {"expressions": ["{{ token }}"]}}]}],
            None,
            id="saved-by-an-earlier-step",
        ),
        # get() and exists() name it in a string: no reference.
        pytest.param([_saving("a", "token", skip_if="{{ flag }}"), {**_STAGE, "request": {"url": "https://x.test/{{ get('token', 'anon') }}"}}], None, id="read-with-get"),
    ],
)
def test_potentially_undefined_only_where_every_saver_may_skip(stages, flagged):
    found = [d.message for d in _check(stages, substitutions=[{"vars": {"flag": True}}]) if d.code == C.UNDEFINED_VAR]
    if flagged is None:
        assert found == []
    else:
        assert len(found) == 1, found
        assert flagged in found[0]


def test_builtin_named_save_of_a_stage_that_may_skip_says_what_is_read_instead():
    diags = _check([_saving("clock", "timestamp", skip_if=True), {**_STAGE, "request": {"url": "https://x.test/?since={{ timestamp }}"}}])
    assert [d.message for d in diags if d.code == C.UNDEFINED_VAR] == [
        "Stage 's': request references 'timestamp', which only stage 'clock' saves, and it has skip_if: when it skips, 'timestamp' is undefined here "
        "— read it with get('timestamp', <default>); where no definition of 'timestamp' is in scope, the name is the template built-in function, "
        "not a value, and a template that renders to it fails the stage"
    ]


def test_uncalled_builtin_in_skip_if_is_reported_there():
    diags = [d for d in _check([{**_STAGE, "skip_if": "{{ str(now) == '' }}"}]) if d.code == C.UNCALLED_BUILTIN]
    assert [d.location for d in diags] == ["stages[0].skip_if"]


@pytest.mark.parametrize("status", ["{{ nope }}", ["{{ nope }}", 304], "{{ [nope, 304] }}"], ids=["whole", "list-entry", "list-rendered"])
def test_status_templates_are_checked_in_every_form(status):
    """A status list entry is read as the whole field is: the runtime renders
    both against the response step's scope."""
    diags = _check([{**_STAGE, "response": [{"verify": {"status": status}}]}])
    undefined = [(d.location, d.message) for d in diags if d.code == DiagnosticCode.UNDEFINED_VAR]
    assert undefined == [("stages[0].response", "Stage 's': response references potentially undefined variable(s): ['nope']")]


@pytest.mark.parametrize(
    "jmespath",
    [
        pytest.param({"id": "{{ nope }}"}, id="value"),
        pytest.param({"tags": ["{{ nope }}"]}, id="array-entry"),
        pytest.param({"id": {"gt": "{{ nope }}"}}, id="matcher-key"),
        pytest.param({"meta": {"eq": {"page": "{{ nope }}"}}}, id="eq-object-member"),
    ],
)
def test_jmespath_templates_are_checked_in_the_response_step_scope(jmespath):
    """``verify.jmespath`` values render in the response step's scope, as
    every verify field's do, wherever in the value the template sits."""
    diags = _check([{**_STAGE, "response": [{"verify": {"jmespath": jmespath}}]}])
    assert [(d.code, d.location, d.message) for d in diags] == [
        (DiagnosticCode.UNDEFINED_VAR, "stages[0].response", "Stage 's': response references potentially undefined variable(s): ['nope']")
    ]


def test_jmespath_templates_see_what_a_response_step_sees():
    """The response namespace and a prior step's save are in scope; a later
    step's save is a forward reference, as the runtime would find it."""
    response = [
        {"save": {"jmespath": {"owner": "data.owner"}}},
        {"verify": {"jmespath": {"data.id": "{{ owner }}", "code": "{{ response.status }}", "next": "{{ later }}"}}},
        {"save": {"jmespath": {"later": "data.next"}}},
    ]
    diags = _check([{**_STAGE, "response": response}])
    assert [(d.code, d.location, d.message) for d in diags] == [
        (DiagnosticCode.FORWARD_REF, "stages[0].response", "Stage 's': response step references 'later' before the save that produces it — steps resolve in order")
    ]


@pytest.mark.parametrize("key", ["'{{ nope }}'", '"{{ nope }}"'], ids=["string-literal", "quoted-field-name"])
def test_jmespath_key_is_never_a_reference(key):
    """A key is never rendered: a template in one that compiles as JMESPath (a
    string literal, a quoted field name) is HTTPCHAIN029's, not a reference to
    an undefined name, and it is evaluated as written, not sent."""
    diags = _check([{**_STAGE, "response": [{"verify": {"jmespath": {key: "x"}}}]}])
    assert [(d.code, d.location, d.message) for d in diags] == [
        (
            DiagnosticCode.TEMPLATE_IN_KEY,
            "stages[0].response[0].verify.jmespath",
            f"Key {key!r} contains a template expression, but a verify.jmespath key is never rendered — JMESPath evaluates it as written. "
            "Write the template in the value the key maps to.",
        )
    ]


def test_key_named_jmespath_elsewhere_is_sent_literally():
    """Only a verify step's jmespath holds expressions: a request body object
    under keys that happen to read verify.jmespath is sent as any other."""
    request = {"url": "http://server/x", "method": "POST", "body": {"json": {"verify": {"jmespath": {"{{ k }}": 1}}}}}
    diags = _check([{**_STAGE, "request": request}])
    assert [(d.code, d.message) for d in diags] == [
        (
            DiagnosticCode.TEMPLATE_IN_KEY,
            "Key '{{ k }}' contains a template expression, but only values are substituted — the key is sent literally. "
            "Move the dynamic part into the value, or build the object in a user function.",
        )
    ]


@pytest.mark.parametrize("jmespath", [{"id": 1}, {"id": None}, {"id": {"ne": None}}], ids=["value", "null", "matcher"])
def test_jmespath_alone_is_an_assertion(jmespath):
    """HTTPCHAIN006: a verify step with only jmespath asserts something."""
    diags = _check([{**_STAGE, "response": [{"verify": {"jmespath": jmespath}}]}])
    assert diags == []


@pytest.mark.parametrize(
    ("matcher", "contradicts"),
    [
        pytest.param({"contains": "x", "not_contains": "x"}, True, id="same-substring"),
        pytest.param({"contains": [1, {"a": 2}], "not_contains": [1.0, {"a": 2}]}, True, id="equal-as-json"),
        # null is an operand: an array must both hold and lack a null.
        pytest.param({"contains": None, "not_contains": None}, True, id="both-null"),
        pytest.param({"contains": "{{ response.status }}", "not_contains": "{{ response.status }}"}, True, id="same-template"),
        pytest.param({"contains": True, "not_contains": 1}, False, id="true-is-not-1"),
        pytest.param({"contains": "1", "not_contains": 1}, False, id="text-is-not-a-number"),
        pytest.param({"contains": None}, False, id="not_contains-unset"),
        pytest.param({"matches": "^A", "not_matches": "^a"}, False, id="different-patterns"),
    ],
)
def test_jmespath_contains_contradiction(matcher, contradicts):
    """HTTPCHAIN007 for a jmespath matcher: contains and not_contains the same
    JSON value, as the check compares them, can never both hold."""
    diags = _check([{**_STAGE, "response": [{"verify": {"jmespath": {"items": matcher}}}]}])
    assert [(d.code, d.location) for d in diags] == ([(DiagnosticCode.CONTAINS_CONTRADICTION, "stages[0].response[0].verify.jmespath.items")] if contradicts else [])


def test_empty_jmespath_asserts_nothing():
    diags = _check([{**_STAGE, "response": [{"verify": {"jmespath": {}}}]}])
    assert [(d.code, d.message) for d in diags] == [
        (DiagnosticCode.NOOP_VERIFY, "Stage 's': verify step asserts nothing (no status, headers, jmespath, expressions, user functions, or body checks)")
    ]


def test_dataflow_locations_are_indexed_json_paths():
    """`Diagnostic.location` is documented as a machine-routable address, so an
    unnamed stage must still produce a usable one (it used to be "")."""
    diags = _check([{"request": {"url": "https://x.test/{{ nope }}"}, "response": [{"verify": {"status": 200}}]}])
    assert all(d.location for d in diags), [d for d in diags if not d.location]
    assert {d.location for d in diags if d.code == DiagnosticCode.UNDEFINED_VAR} == {"stages[0].request"}


def test_reserved_marker_name_is_diagnostic_not_crash():
    """pytest's MarkGenerator raises AttributeError for underscore-prefixed
    names; the validator must report INVALID_MARKER, not blow up (only
    ValueError/SyntaxError used to be caught)."""
    diags = _check([{**_STAGE, "marks": ["_foo"]}])
    assert any(d.code == DiagnosticCode.INVALID_MARKER and d.severity == "error" for d in diags), diags


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        # Template references (extract_template_variables): the name at the bottom is still found.
        pytest.param({"request": {"url": "https://x.test/", "params": {"p": nested("{{ nowhere }}", BEYOND_RECURSION_LIMIT)}}}, C.UNDEFINED_VAR, id="reference"),
        # Templated keys (029) are searched in the raw JSON.
        pytest.param({"request": {"url": "https://x.test/", "params": {"p": nested({"{{ k }}": 1}, BEYOND_RECURSION_LIMIT)}}}, C.TEMPLATE_IN_KEY, id="templated-key"),
        # The schema meta-check never descends into `enum`, so this reaches the directive search (028).
        pytest.param(
            {"response": [{"verify": {"body": {"schema": {"enum": [nested(1, BEYOND_RECURSION_LIMIT)], "properties": {"a": {"$include": "a.json"}}}}}}]},
            C.SCHEMA_SCENARIO_DIRECTIVE,
            id="schema-directive",
        ),
        # contains_template decides that parametrize values resolve at collection (025).
        pytest.param({"parametrize": [{"individual": {"p": [nested("{{ 1 }}", BEYOND_RECURSION_LIMIT)]}}]}, C.PARAMETRIZE_COLLECTION_RESOLUTION, id="parametrize-template"),
    ],
)
def test_checks_reach_values_of_any_depth(fields, expected):
    """Every walk over scenario values is iterative. The recursive ones crashed
    `validate` and collection with a RecursionError on a value a few hundred
    levels deep, which loads fine."""
    assert expected in {d.code for d in _check([{**_STAGE, **fields}])}
