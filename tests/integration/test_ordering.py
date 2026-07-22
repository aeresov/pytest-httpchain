"""Chain contiguity in a single (non-xdist) pytest session.

Each scenario's stages form one ordered chain over shared class state, and
pytest finalizes class scope (``Carrier.teardown_class`` — which resets the
saved context) every time execution leaves the class. So the engine's ordering
invariant is: all items of a scenario class run contiguously, in stage order,
regardless of how other plugins sort the collected items.

The pathological sorter is pytest-order itself: every scenario class carries
the same ``order(0..n-1)`` stage marks, and pytest-order's default
session-wide group scope stable-sorts equal indices across classes into
A0, B0, A1, B1, ... — wiping each chain's context between its own stages.

The scenarios here are the same strictly-chained trio as the xdist stress
test: six stages each, stage k verifies ``x == base + k - 1`` before
incrementing, so any interleaving, reordering, or context reset fails
immediately.
"""

CHAIN_SCENARIOS = [
    "xdist/test_chain_a.http.json",
    "xdist/test_chain_b.http.json",
    "xdist/test_chain_c.http.json",
]


def run_chains(pytester, *args):
    pytester.copy_example("conftest.py")
    for scenario in CHAIN_SCENARIOS:
        pytester.copy_example(scenario)
    return pytester.runpytest_subprocess(*args)


def test_multiple_scenarios_plain_run(pytester):
    """Several multi-stage scenarios in one plain pytest run stay contiguous."""
    result = run_chains(pytester)
    result.assert_outcomes(passed=18)


def test_multiple_scenarios_without_pytest_order(pytester):
    """The invariant must not depend on pytest-order being active."""
    result = run_chains(pytester, "-p", "no:order")
    result.assert_outcomes(passed=18)


def test_items_reordered_by_another_plugin(pytester):
    """Stage order survives arbitrary reordering by other plugins.

    A plain hookimpl runs before the plugin's regrouping wrapper; reversing
    the item list scrambles both inter-class and intra-class order. pytest-order
    is disabled so nothing repairs the scramble first — this passes only if
    the regroup itself restores stage order within each class.
    """
    pytester.makepyfile(
        reverser="""
        def pytest_collection_modifyitems(items):
            items.reverse()
        """
    )
    result = run_chains(pytester, "-p", "no:order", "-p", "reverser")
    result.assert_outcomes(passed=18)


def test_parametrized_stage_instances_keep_order(pytester):
    """Parametrized instances of one stage keep collection order through regrouping.

    The last instance's save is what later stages consume, so instance order
    is semantically load-bearing: restoring stage order alone is not enough —
    the regroup must also undo a shuffler's scramble within a stage.
    """
    pytester.copy_example("conftest.py")
    pytester.copy_example("ordering/test_chain_param.http.json")
    pytester.makepyfile(
        reverser="""
        def pytest_collection_modifyitems(items):
            items.reverse()
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:order", "-p", "reverser")
    result.assert_outcomes(passed=3)


def test_failed_first_keeps_chains_contiguous(pytester):
    """A partially-failed chain replays identically under pytest --ff.

    Core's cacheprovider (LFPlugin) reorders in a tryfirst wrapper whose
    post-yield runs AFTER the plugin's own modifyitems wrapper, so contiguity
    must be re-enforced later (pytest_collection_finish) — otherwise --ff
    moves the failed mid-chain stage ahead of stage 0.
    """
    pytester.copy_example("conftest.py")
    pytester.copy_example("ordering/test_chain_fail.http.json")
    first = pytester.runpytest_subprocess()
    first.assert_outcomes(passed=1, failed=1, skipped=1)
    again = pytester.runpytest_subprocess("--ff")
    again.assert_outcomes(passed=1, failed=1, skipped=1)
