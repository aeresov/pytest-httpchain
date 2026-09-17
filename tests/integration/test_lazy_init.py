"""Pure collection: scenario initialization is deferred to first stage execution.

Collection (``--collect-only``, IDE discovery) must not execute user code —
scenario ``auth`` functions and user functions called from scenario-level
substitution templates — nor build httpx clients. The sentinel user functions
in ``lazy_init/sentinel.py`` touch marker files when invoked, so each test can
assert exactly which phase ran them.

Documented exception: when stage ``parametrize`` values contain templates,
scenario substitutions must still resolve at collection because pytest needs
concrete parameter values to generate test items.
"""

from pathlib import Path

# Every scenario here pairs with the sentinel user functions.
SENTINEL = "lazy_init/sentinel.py"


def test_collect_only_does_not_call_auth(pytester, run_scenario):
    result = run_scenario("lazy_init/test_lazy_auth.http.json", SENTINEL, args="--collect-only")
    assert result.ret == 0
    assert not Path(pytester.path, "auth_called.txt").exists()


def test_collect_only_does_not_resolve_substitutions(pytester, run_scenario):
    result = run_scenario("lazy_init/test_lazy_substitutions.http.json", SENTINEL, args="--collect-only")
    assert result.ret == 0
    assert not Path(pytester.path, "token_called.txt").exists()


def test_auth_called_at_execution(pytester, run_scenario):
    result = run_scenario("lazy_init/test_lazy_auth.http.json", SENTINEL)
    result.assert_outcomes(passed=1)
    assert Path(pytester.path, "auth_called.txt").exists()


def test_substitutions_resolved_at_execution(pytester, run_scenario):
    result = run_scenario("lazy_init/test_lazy_substitutions.http.json", SENTINEL)
    result.assert_outcomes(passed=1)
    assert Path(pytester.path, "token_called.txt").exists()


def test_auth_failure_fails_first_stage_cleanly(run_scenario):
    """A broken auth function fails the first stage with a clean message and
    aborts the chain — no collection error, no internal traceback."""
    result = run_scenario("lazy_init/test_lazy_auth_failure.http.json", SENTINEL)
    result.assert_outcomes(failed=1, skipped=1)
    result.stdout.fnmatch_lines(["*Failed to initialize scenario*token service unreachable*"])
    result.stdout.no_fnmatch_line("*INTERNALERROR*")


def test_always_run_stage_skips_after_init_failure(run_scenario):
    """Init failure makes the whole scenario unusable: even always_run stages
    skip (matching the old eager behavior where nothing ran at all), instead of
    evaluating templates against the empty context the failure left behind."""
    result = run_scenario("lazy_init/test_lazy_always_run.http.json", SENTINEL, args=("-s", "-rs"))
    result.assert_outcomes(failed=1, skipped=1)
    result.stdout.fnmatch_lines(["*Scenario initialization failed*"])


def test_init_failure_not_silently_absorbed_by_xfail_marks(run_scenario):
    """A broken scenario whose stages are all xfail-marked must not report a
    green all-xfail run. Initialization failure is scenario-level breakage,
    not the stage-level "expected failure" the mark declares: the plugin
    overrides the xfail report to a real FAILURE (pre-0.10 this was a hard
    collection error regardless of marks), and every later stage skips with
    the root cause."""
    result = run_scenario("lazy_init/test_lazy_xfail.http.json", SENTINEL, args=("-s", "-rs"))
    result.assert_outcomes(failed=1, skipped=1)
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*Failed to initialize scenario*token service unreachable*"])


def test_substitutions_run_at_most_once_after_init_failure(pytester, run_scenario):
    """Initialization never retries: substitutions resolve, then auth raises —
    the counting substitution function must have run exactly once even though
    a second (always_run) stage was executed (and skipped)."""
    result = run_scenario("lazy_init/test_lazy_once.http.json", SENTINEL)
    result.assert_outcomes(failed=1, skipped=1)
    calls = Path(pytester.path, "count_calls.txt")
    assert calls.exists()
    assert calls.read_text().count("called") == 1


def test_template_ids_do_not_force_collection_context(pytester, run_scenario):
    """Templates in parametrize `ids` are never walked, so they must not trigger
    collection-time resolution of scenario substitutions (the `tok` sentinel)."""
    result = run_scenario("lazy_init/test_lazy_ids.http.json", SENTINEL, args=("--collect-only", "-q"))
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*2 tests collected*"])
    assert not Path(pytester.path, "token_called.txt").exists()


def test_template_parametrize_still_resolves_at_collection(pytester, run_scenario):
    """The documented exception: template parametrize values force scenario
    substitutions to resolve at collection (pytest needs concrete values)."""
    result = run_scenario("lazy_init/test_lazy_parametrize.http.json", SENTINEL, args=("--collect-only", "-q"))
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*3 tests collected*"])
    assert Path(pytester.path, "mk_envs_called.txt").exists()


def test_collection_context_always_run_does_not_force_init(pytester, run_scenario):
    """A template `always_run` must not initialize when the context is already
    there. Template parametrize values resolve scenario substitutions at
    collection, so a false `always_run` can be evaluated with no initialization
    at all — and auth must stay uncalled for stages that only ever skip."""
    result = run_scenario("lazy_init/test_lazy_always_run_collection_context.http.json", SENTINEL, args=("-s", "-rs"))
    result.assert_outcomes(errors=1, skipped=4)
    result.stdout.fnmatch_lines(["*Flow aborted*"])
    assert Path(pytester.path, "mk_envs_called.txt").exists(), "parametrize templates resolve substitutions at collection"
    assert not Path(pytester.path, "auth_called.txt").exists(), "auth must not run for a stage that only skips"


def test_template_parametrize_executes_correctly(run_scenario):
    result = run_scenario("lazy_init/test_lazy_parametrize.http.json", SENTINEL)
    result.assert_outcomes(passed=3)
