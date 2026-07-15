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


def _setup(pytester, scenario):
    pytester.copy_example("conftest.py")
    pytester.copy_example("lazy_init/sentinel.py")
    pytester.copy_example(scenario)


def test_collect_only_does_not_call_auth(pytester):
    _setup(pytester, "lazy_init/test_lazy_auth.http.json")
    result = pytester.runpytest("--collect-only")
    assert result.ret == 0
    assert not Path(pytester.path, "auth_called.txt").exists()


def test_collect_only_does_not_resolve_substitutions(pytester):
    _setup(pytester, "lazy_init/test_lazy_substitutions.http.json")
    result = pytester.runpytest("--collect-only")
    assert result.ret == 0
    assert not Path(pytester.path, "token_called.txt").exists()


def test_auth_called_at_execution(pytester):
    _setup(pytester, "lazy_init/test_lazy_auth.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)
    assert Path(pytester.path, "auth_called.txt").exists()


def test_substitutions_resolved_at_execution(pytester):
    _setup(pytester, "lazy_init/test_lazy_substitutions.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)
    assert Path(pytester.path, "token_called.txt").exists()


def test_auth_failure_fails_first_stage_cleanly(pytester):
    """A broken auth function fails the first stage with a clean message and
    aborts the chain — no collection error, no internal traceback."""
    _setup(pytester, "lazy_init/test_lazy_auth_failure.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(failed=1, skipped=1)
    result.stdout.fnmatch_lines(["*Failed to initialize scenario*token service unreachable*"])
    result.stdout.no_fnmatch_line("*INTERNALERROR*")


def test_always_run_stage_skips_after_init_failure(pytester):
    """Init failure makes the whole scenario unusable: even always_run stages
    skip (matching the old eager behavior where nothing ran at all), instead of
    evaluating templates against the empty context the failure left behind."""
    _setup(pytester, "lazy_init/test_lazy_always_run.http.json")
    result = pytester.runpytest("-s", "-rs")
    result.assert_outcomes(failed=1, skipped=1)
    result.stdout.fnmatch_lines(["*Scenario initialization failed*"])


def test_init_failure_not_silently_absorbed_by_xfail_marks(pytester):
    """A broken scenario whose stages are all xfail-marked must not report a
    green all-xfail run. Initialization failure is scenario-level breakage,
    not the stage-level "expected failure" the mark declares: the plugin
    overrides the xfail report to a real FAILURE (pre-0.10 this was a hard
    collection error regardless of marks), and every later stage skips with
    the root cause."""
    _setup(pytester, "lazy_init/test_lazy_xfail.http.json")
    result = pytester.runpytest("-s", "-rs")
    result.assert_outcomes(failed=1, skipped=1)
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*Failed to initialize scenario*token service unreachable*"])


def test_substitutions_run_at_most_once_after_init_failure(pytester):
    """Initialization never retries: substitutions resolve, then auth raises —
    the counting substitution function must have run exactly once even though
    a second (always_run) stage was executed (and skipped)."""
    _setup(pytester, "lazy_init/test_lazy_once.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(failed=1, skipped=1)
    calls = Path(pytester.path, "count_calls.txt")
    assert calls.exists()
    assert calls.read_text().count("called") == 1


def test_template_ids_do_not_force_collection_context(pytester):
    """Templates in parametrize `ids` are never walked, so they must not trigger
    collection-time resolution of scenario substitutions (the `tok` sentinel)."""
    _setup(pytester, "lazy_init/test_lazy_ids.http.json")
    result = pytester.runpytest("--collect-only", "-q")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*2 tests collected*"])
    assert not Path(pytester.path, "token_called.txt").exists()


def test_template_parametrize_still_resolves_at_collection(pytester):
    """The documented exception: template parametrize values force scenario
    substitutions to resolve at collection (pytest needs concrete values)."""
    _setup(pytester, "lazy_init/test_lazy_parametrize.http.json")
    result = pytester.runpytest("--collect-only", "-q")
    assert result.ret == 0
    result.stdout.fnmatch_lines(["*3 tests collected*"])
    assert Path(pytester.path, "mk_envs_called.txt").exists()


def test_template_parametrize_executes_correctly(pytester):
    _setup(pytester, "lazy_init/test_lazy_parametrize.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=3)
