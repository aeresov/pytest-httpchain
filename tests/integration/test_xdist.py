"""pytest-xdist compatibility.

A scenario's stages form one ordered chain over shared class state, so xdist
modes that distribute tests individually (``load`` — the ``-n`` default —
``each``, ``worksteal``) must be rejected at collection instead of letting
chains break silently. Class-preserving modes work: ``loadscope``/``loadfile``
group by class/file, and ``loadgroup`` works because every scenario class gets
an automatic ``xdist_group`` marker.

The scenario used here chains two stages (stage 2 consumes stage 1's saved
values), so a split across workers cannot pass by accident.
"""

import pytest

SCENARIO = "save/test_save_jmespath.http.json"


def run_xdist(pytester, *args):
    pytester.copy_example("conftest.py")
    pytester.copy_example(SCENARIO)
    return pytester.runpytest_subprocess(*args)


def test_single_stage_allowed_under_any_mode(pytester):
    """A single-stage scenario has no chain to split — bare -n keeps working."""
    pytester.copy_example("conftest.py")
    pytester.copy_example("xdist/test_single.http.json")
    result = pytester.runpytest_subprocess("-n", "2")
    result.assert_outcomes(passed=1)


@pytest.mark.parametrize("mode", ["load", "each", "worksteal"])
def test_chain_splitting_modes_rejected(pytester, mode):
    """Modes that can scatter one scenario's stages across workers fail collection."""
    result = run_xdist(pytester, "-n", "2", "--dist", mode)
    outcomes = result.parseoutcomes()
    assert outcomes.get("passed", 0) == 0, "no stage may run under an incompatible dist mode"
    assert result.ret != 0
    result.stdout.fnmatch_lines([f"*cannot run under pytest-xdist --dist={mode}*"])
    result.stdout.fnmatch_lines(["*loadscope*"])


def test_bare_numprocesses_rejected(pytester):
    """-n without --dist implies the incompatible default mode (load)."""
    result = run_xdist(pytester, "-n", "2")
    assert result.parseoutcomes().get("passed", 0) == 0
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*cannot run under pytest-xdist --dist=load*"])


@pytest.mark.parametrize("mode", ["loadscope", "loadfile", "loadgroup"])
def test_class_preserving_modes_supported(pytester, mode):
    """Modes that keep a class/file/group on one worker run the chain correctly."""
    result = run_xdist(pytester, "-n", "2", "--dist", mode)
    result.assert_outcomes(passed=2)


def test_xdist_installed_but_inactive(pytester):
    """With xdist installed but no -n, behavior is unchanged."""
    result = run_xdist(pytester)
    result.assert_outcomes(passed=2)


CHAIN_SCENARIOS = [
    "xdist/test_chain_a.http.json",
    "xdist/test_chain_b.http.json",
    "xdist/test_chain_c.http.json",
]


@pytest.mark.parametrize("mode", ["loadscope", "loadfile", "loadgroup"])
def test_stage_order_strict_across_scenarios(pytester, mode):
    """Ordering stress: pytest-order must sequence stages inside each worker.

    Three scenarios of six strictly-chained stages each — stage k verifies
    ``x == base + k - 1`` before incrementing, so any stage that runs out of
    order, lands on the wrong worker, or reads another scenario's context
    (the three scenarios use disjoint bases) fails immediately. With -n 2 at
    least one worker receives two scenario groups, exercising group-after-group
    sequencing as well.
    """
    pytester.copy_example("conftest.py")
    for scenario in CHAIN_SCENARIOS:
        pytester.copy_example(scenario)
    result = pytester.runpytest_subprocess("-n", "2", "--dist", mode)
    result.assert_outcomes(passed=18)
