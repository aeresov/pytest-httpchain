"""Chain contiguity in a single (non-xdist) pytest session.

Each scenario's stages form one ordered chain over shared class state, and
pytest finalizes class scope (``Carrier.teardown_class`` — which resets the
saved context) every time execution leaves the class. So the engine's ordering
invariant is: all items of a scenario class run contiguously, in stage order,
regardless of how other plugins sort the collected items.

The canonical pathological sorter is a user-installed pytest-order (a dev
dependency here, exactly for these tests) acting on user-authored
``order(...)`` stage marks: with matching indices across scenarios, its
default session-wide group scope stable-sorts equal indices across classes
into A0, B0, A1, B1, ... — wiping each chain's context between its own
stages.

The scenarios here are the same strictly-chained trio as the xdist stress
test: six stages each, stage k verifies ``x == base + k - 1`` before
incrementing, so any interleaving, reordering, or context reset fails
immediately.
"""

import pytest

# Every test here spawns pytester subprocesses (runpytest_subprocess).
pytestmark = pytest.mark.slow

CHAIN_SCENARIOS = [
    "xdist/test_chain_a.http.json",
    "xdist/test_chain_b.http.json",
    "xdist/test_chain_c.http.json",
]


def run_chains(run_scenario, *args):
    return run_scenario(*CHAIN_SCENARIOS, args=args, subprocess=True)


def test_multiple_scenarios_plain_run(run_scenario):
    """Several multi-stage scenarios in one plain pytest run stay contiguous."""
    result = run_chains(run_scenario)
    result.assert_outcomes(passed=18)


def test_user_order_marks_with_pytest_order(run_scenario):
    """User-authored ``order(...)`` marks plus active pytest-order cannot split chains.

    Both scenarios mark their stages ``order(0..2)``; pytest-order's default
    session-wide sort interleaves the classes (A0, B0, A1, B1, ...), and the
    regroup must restore per-class contiguity.
    """
    result = run_scenario(
        "ordering/test_chain_marks_a.http.json",
        "ordering/test_chain_marks_b.http.json",
        args=(),
        subprocess=True,
    )
    result.assert_outcomes(passed=6)


def test_items_reordered_by_another_plugin(pytester, run_scenario):
    """Stage order survives arbitrary reordering by other plugins.

    A plain hookimpl runs before the plugin's regrouping wrapper; reversing
    the item list scrambles both inter-class and intra-class order. The chain
    scenarios carry no order marks, so nothing repairs the scramble first —
    this passes only if the regroup itself restores stage order within each
    class.
    """
    pytester.makepyfile(
        reverser="""
        def pytest_collection_modifyitems(items):
            items.reverse()
        """
    )
    result = run_chains(run_scenario, "-p", "reverser")
    result.assert_outcomes(passed=18)


def test_parametrized_stage_instances_keep_order(pytester, run_scenario):
    """Parametrized instances of one stage keep collection order through regrouping.

    The last instance's save is what later stages consume, so instance order
    is semantically load-bearing: restoring stage order alone is not enough —
    the regroup must also undo a shuffler's scramble within a stage.
    """
    pytester.makepyfile(
        reverser="""
        def pytest_collection_modifyitems(items):
            items.reverse()
        """
    )
    result = run_scenario("ordering/test_chain_param.http.json", args=("-p", "reverser"), subprocess=True)
    result.assert_outcomes(passed=3)


def test_failed_first_keeps_chains_contiguous(pytester, run_scenario):
    """A partially-failed chain replays identically under pytest --ff.

    Core's cacheprovider (LFPlugin) reorders in a tryfirst wrapper whose
    post-yield runs AFTER the plugin's own modifyitems wrapper, so contiguity
    must be re-enforced later (pytest_collection_finish) — otherwise --ff
    moves the failed mid-chain stage ahead of stage 0.
    """
    first = run_scenario("ordering/test_chain_fail.http.json", args=(), subprocess=True)
    first.assert_outcomes(passed=1, failed=1, skipped=1)
    again = pytester.runpytest_subprocess("--ff")
    again.assert_outcomes(passed=1, failed=1, skipped=1)


def test_selection_dropping_earlier_stages_warns(run_scenario):
    """Reordering is defeated and chain-splitting dist modes are rejected, but
    pytest's selection mechanisms (-k, --lf, --deselect) can still silently
    orphan a chain's tail. Selecting only a later stage must warn that the
    survivors run without the deselected stages' saved context."""
    result = run_scenario("save/test_save_jmespath.http.json", args=("-s", "-k", "use_saved_values"))
    result.stdout.fnmatch_lines(["*were deselected*"])


def test_full_chain_selection_does_not_warn(run_scenario):
    result = run_scenario("save/test_save_jmespath.http.json")
    result.assert_outcomes(passed=2)
    result.stdout.no_fnmatch_line("*were deselected*")


def test_split_chain_warning_survives_filterwarnings_error(pytester, run_scenario):
    """The split-chain warning fires from pytest_collection_finish, which has no
    warning-to-error recovery: under `filterwarnings = error` an escaping
    warning ended the session in an INTERNALERROR traceback. It must fail
    cleanly instead, honoring the user's policy without crashing pytest."""
    pytester.makeini("[pytest]\nfilterwarnings =\n    error\n")

    result = run_scenario("save/test_save_jmespath.http.json", args=("-k", "use_saved_values"), subprocess=True)

    result.stdout.no_fnmatch_line("*INTERNALERROR*")
    # A UsageError: pytest renders it on stderr and exits 4, rather than the
    # pluggy traceback and exit 3 an escaping warning produced.
    result.stderr.fnmatch_lines(["*were deselected*"])
    assert result.ret == pytest.ExitCode.USAGE_ERROR
