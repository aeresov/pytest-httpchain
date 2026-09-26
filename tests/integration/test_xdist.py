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

# Every test here spawns pytester subprocesses (runpytest_subprocess) with their
# own per-stage HTTP servers — the slowest family in the suite.
pytestmark = pytest.mark.slow

SCENARIO = "save/test_save_jmespath.http.json"


def run_xdist(run_scenario, *args):
    return run_scenario(SCENARIO, args=args, subprocess=True)


def test_single_stage_allowed_under_any_mode(run_scenario):
    """A single-stage scenario has no chain to split — bare -n keeps working."""
    result = run_scenario("xdist/test_single.http.json", args=("-n", "2"), subprocess=True)
    result.assert_outcomes(passed=1)


@pytest.mark.parametrize(
    ("dist_args", "mode"),
    [
        pytest.param(("--dist", "load"), "load", id="load"),
        pytest.param(("--dist", "each"), "each", id="each"),
        pytest.param(("--dist", "worksteal"), "worksteal", id="worksteal"),
        pytest.param((), "load", id="bare-n"),  # -n alone implies load
    ],
)
def test_chain_splitting_modes_rejected(run_scenario, dist_args, mode):
    """Modes that can scatter one scenario's stages across workers fail collection."""
    result = run_xdist(run_scenario, "-n", "2", *dist_args)
    assert result.parseoutcomes().get("passed", 0) == 0, "no stage may run under an incompatible dist mode"
    assert result.ret != 0
    result.stdout.fnmatch_lines([f"*cannot run under pytest-xdist --dist={mode}*Use --dist loadscope*"])


@pytest.mark.parametrize("mode", ["loadscope", "loadfile", "loadgroup"])
def test_class_preserving_modes_supported(run_scenario, mode):
    """Modes that keep a class/file/group on one worker run the chain correctly."""
    result = run_xdist(run_scenario, "-n", "2", "--dist", mode)
    result.assert_outcomes(passed=2)


def test_xdist_installed_but_inactive(run_scenario):
    """With xdist installed but no -n, behavior is unchanged."""
    result = run_xdist(run_scenario)
    result.assert_outcomes(passed=2)


CHAIN_SCENARIOS = [
    "xdist/test_chain_a.http.json",
    "xdist/test_chain_b.http.json",
    "xdist/test_chain_c.http.json",
]


@pytest.mark.parametrize("mode", ["loadscope", "loadfile", "loadgroup"])
def test_stage_order_strict_across_scenarios(run_scenario, mode):
    """Ordering stress: the plugin's collection hooks must sequence stages inside each worker.

    Three scenarios of six strictly-chained stages each — stage k verifies
    ``x == base + k - 1`` before incrementing, so any stage that runs out of
    order, lands on the wrong worker, or reads another scenario's context
    (the three scenarios use disjoint bases) fails immediately. With -n 2 at
    least one worker receives two scenario groups, exercising group-after-group
    sequencing as well.
    """
    result = run_scenario(*CHAIN_SCENARIOS, args=("-n", "2", "--dist", mode), subprocess=True)
    result.assert_outcomes(passed=18)
