import re
from pathlib import Path

import pytest

from tests.integration.helpers import named, stage


@pytest.mark.parametrize(
    ("scenario", "passed"),
    named(
        ("fixture_injection", 1),
        ("fixture_dict", 1),
        ("fixture_factory", 1),  # a callable fixture (factory pattern)
        # Scenario-level fixtures reach every stage, deduplicated against the
        # stage's own fixtures.
        ("scenario_fixtures", 2),
    ),
)
def test_fixture_values_reach_templates(run_scenario, scenario, passed):
    run_scenario(f"fixtures/test_{scenario}.http.json").assert_outcomes(passed=passed)


@pytest.mark.parametrize(
    ("call", "outcomes", "line"),
    [
        # Committed while the class-scoped connection it is built on is open.
        pytest.param("transaction('t1')", {"passed": 2}, "transaction t1 committed", id="exited-with-stage"),
        # An error on exit fails the stage: it aborts the chain, and discards
        # the save, so the cleanup stage guarded by exists() skips.
        pytest.param(
            "transaction('t1', fail=True)",
            {"failed": 1, "skipped": 1},
            "Exiting the context manager from fixture 'transaction' failed: RuntimeError: commit of t1 rejected",
            id="error-on-exit",
        ),
    ],
)
def test_factory_fixture_context_manager_exits_with_its_stage(run_scenario, call, outcomes, line):
    """A context manager a factory fixture returns is exited when the stage that
    entered it ends, before pytest tears down the fixtures it is built on. It
    was exited at class teardown, after even a class-scoped ``connection`` was
    closed, and its error on exit was only logged: both runs passed. The
    cleanup stage, guarded by ``exists()``, pins that a stage failed on exit
    discards its save like any failed stage, so no cleanup runs for a
    transaction that was never committed."""
    begin = stage(
        "begin",
        "/template/{{ " + call + " }}",
        response=[{"verify": {"status": 200}}, {"save": {"substitutions": [{"vars": {"begun": True}}]}}],
        fixtures=["server", "transaction"],
    )
    scenario = {"stages": [begin, stage("cleanup", always_run="{{ exists('begun') }}")]}
    result = run_scenario(scenario)
    result.assert_outcomes(**outcomes)
    result.stdout.fnmatch_lines([f"*{line}*"])


def _per_tenant(*stages):
    """A scenario whose every stage requests the class-scoped ``tenant`` fixture
    (params alpha, beta), so it runs as one chain per tenant."""
    return {"fixtures": ["tenant"], "stages": list(stages)}


def test_high_scoped_fixture_params_run_one_chain_each(run_scenario):
    """A class-scoped fixture with params, requested by every stage, runs the
    whole chain once per param, setting the fixture up once each. Sorting by
    stage alone ran create[alpha], create[beta], read[alpha], read[beta]:
    read[alpha] saw beta's save and failed, read[beta] skipped, and the fixture
    was set up once per stage."""
    scenario = _per_tenant(
        stage("create", "/template/{{ tenant }}", response=[{"verify": {"status": 200}}, {"save": {"jmespath": {"created_for": "value"}}}]),
        stage("read", response=[{"verify": {"expressions": ["{{ created_for == tenant }}"]}}]),
    )
    result = run_scenario(scenario, args=("-s", "-v"))
    result.assert_outcomes(passed=4)
    result.stdout.fnmatch_lines(["*create[[]alpha[]]*", "*read[[]alpha[]]*", "*create[[]beta[]]*", "*read[[]beta[]]*"])
    # Under -v a print shares its line with the test id.
    assert re.findall(r"tenant setup: (\w+)", result.stdout.str()) == ["alpha", "beta"]


@pytest.mark.parametrize(
    ("scenario", "outcomes"),
    [
        # The saved context: the second chain must not see the first one's save.
        pytest.param(
            _per_tenant(
                stage("create", response=[{"verify": {"expressions": ["{{ not exists('created') }}"]}}, {"save": {"substitutions": [{"vars": {"created": True}}]}}]),
                stage("read", response=[{"verify": {"expressions": ["{{ created }}"]}}]),
            ),
            {"passed": 4},
            id="saves",
        ),
        # The abort flag: the first chain's failure must not skip the second.
        pytest.param(
            _per_tenant(
                stage("gate", response=[{"verify": {"expressions": ["{{ tenant == 'beta' }}"]}}]),
                stage("after"),
            ),
            {"passed": 2, "failed": 1, "skipped": 1},
            id="abort",
        ),
        # ...yet an error setting up the second chain's first stage aborts that
        # chain: entering it resets ahead of fixture setup, not at its next stage.
        pytest.param(
            _per_tenant(stage("first", fixtures=["server", "beta_setup_error"]), stage("second")),
            {"passed": 2, "errors": 1, "skipped": 1},
            id="setup-error",
        ),
        # The client: the first chain's cookie must not reach the second.
        pytest.param(
            _per_tenant(
                stage("no_cookie_yet", "/headers", response=[{"verify": {"status": 200, "body": {"not_contains": ["session="]}}}]),
                stage("receive_cookie", "/scoped-cookies"),
            ),
            {"passed": 4},
            id="client",
        ),
    ],
)
def test_each_param_chain_starts_fresh(run_scenario, scenario, outcomes):
    """Each param's chain is a complete run of the scenario, isolated like one:
    class teardown resets the chain only once per class, so the chain boundary
    must reset it too."""
    run_scenario(scenario).assert_outcomes(**outcomes)


@pytest.mark.parametrize(
    ("auth", "outcomes"),
    [
        # Resolved by the first chain, reused by the second.
        pytest.param("sentinel:auth", {"passed": 4}, id="resolved"),
        # Not retried: every later stage skips, in every chain, always_run too.
        pytest.param("sentinel:broken_auth", {"failed": 1, "skipped": 3}, id="failed"),
    ],
)
def test_param_chains_share_the_scenario_initialization(pytester, run_scenario, auth, outcomes):
    """Scenario ``substitutions``, ``auth`` and ``ssl`` see no fixtures, so no
    param changes them, and they resolve at most once per scenario: a user
    function there does not run again for the next chain, not even after it
    failed. Resetting the chain the way class teardown does would re-run them
    per chain."""
    scenario = _per_tenant(stage("first"), stage("cleanup", always_run=True))
    scenario |= {"auth": auth, "substitutions": [{"functions": {"count": "sentinel:count"}}, {"vars": {"c": "{{ count() }}"}}]}
    run_scenario(scenario, "lazy_init/sentinel.py").assert_outcomes(**outcomes)
    assert Path(pytester.path, "count_calls.txt").read_text().count("called") == 1


def test_selection_orphaning_one_param_chain_warns(run_scenario):
    """``--lf`` after read[alpha] and create[beta] failed selects just those two:
    every stage of the scenario is still selected, yet alpha's chain lost its
    head. The split-chain warning is checked per chain, so it fires, naming
    the chain; beta's, whole, is not mentioned."""
    result = run_scenario(_per_tenant(stage("create"), stage("read")), args=("-k", "not (create and alpha)"))
    result.assert_outcomes(passed=3, deselected=1)
    result.stdout.fnmatch_lines(["*earlier stage(s) [[]'create'[]] were deselected*the chain for tenant='alpha' remain selected*"])
    result.stdout.no_fnmatch_line("*tenant='beta'*")


@pytest.mark.parametrize(
    ("selection", "outcomes"),
    [
        pytest.param(("-k", "per_tenant"), {"passed": 2, "deselected": 1}, id="keyword"),
        # How an IDE runs one test. A node id narrows the collection itself, so
        # no hook ever sees the stage without the fixture: the class collector
        # has to record which stages request it.
        pytest.param("test_inline.http.json::inline::test 0 - per_tenant", {"passed": 2}, id="node-id"),
    ],
)
def test_param_chains_do_not_depend_on_selection(run_scenario, selection, outcomes):
    """Selecting only the stage that requests the fixture leaves no stage
    without it. Its params still vary in place within one chain, as in the full
    run, instead of each starting afresh because of the selection."""
    seen_alpha = {"verify": {"expressions": ["{{ tenant == 'alpha' or exists('seen') }}"]}}
    save_seen = {"save": {"substitutions": [{"vars": {"seen": "{{ tenant }}"}}]}}
    scenario = {"stages": [stage("per_tenant", fixtures=["server", "tenant"], response=[seen_alpha, save_seen]), stage("after")]}
    run_scenario(scenario, args=selection).assert_outcomes(**outcomes)


def test_fixture_params_some_stages_request_vary_in_place(run_scenario):
    """A stage that does not request the fixture belongs to no single param, so
    such a fixture does not split the scenario: the stage requesting it runs
    once per param in place, like stage `parametrize`, within one chain that
    shares its saves."""
    has_saved = {"verify": {"expressions": ["{{ created == 'shared' }}"]}}
    scenario = {
        "stages": [
            stage("create", "/template/shared", response=[{"verify": {"status": 200}}, {"save": {"jmespath": {"created": "value"}}}]),
            stage("per_tenant", fixtures=["server", "tenant"], response=[has_saved]),
            stage("after", response=[has_saved]),
        ]
    }
    result = run_scenario(scenario, args="-v")
    result.assert_outcomes(passed=4)
    result.stdout.fnmatch_lines(["*create*", "*per_tenant[[]alpha[]]*", "*per_tenant[[]beta[]]*", "*after*"])
    # One stage's params run back to back, each set up once: nothing to warn of.
    result.stdout.no_fnmatch_line("*does not run once per param*")


def test_fixture_params_several_stages_request_warn(run_scenario):
    """Two stages, but not all, requesting the fixture vary it in place across
    both: each runs for every param before the next one does, so the second
    sees what the first saved for the last param, and the class-scoped fixture
    is set up again at every change of param. That is the ordering a
    scenario-level fixture avoids, so collection warns and names the fix."""
    scenario = {
        "stages": [
            stage("login"),
            stage("create", fixtures=["server", "tenant"]),
            stage("read", fixtures=["server", "tenant"]),
        ]
    }
    result = run_scenario(scenario, args=("-s", "-v"))
    result.assert_outcomes(passed=5, warnings=1)
    result.stdout.fnmatch_lines(["*create[[]alpha[]]*", "*create[[]beta[]]*", "*read[[]alpha[]]*", "*read[[]beta[]]*"])
    assert re.findall(r"tenant setup: (\w+)", result.stdout.str()) == ["alpha", "beta", "alpha", "beta"]
    result.stdout.fnmatch_lines(
        [
            "*Scenario 'test_inline.http.json::inline': the class-scoped fixture 'tenant' has params and is requested by stages [[]'create', 'read'[]] "
            "but not by every stage, so the scenario does not run once per param*Request 'tenant' from every stage*"
        ]
    )
