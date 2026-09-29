"""pytest-xdist compatibility.

A scenario's stages form one ordered chain over shared class state, so xdist
modes that distribute tests individually (``load`` — the ``-n`` default —
``each``, ``worksteal``) must be rejected at collection instead of letting
chains break silently. Class-preserving modes work: ``loadscope``/``loadfile``
group by class/file, and ``loadgroup`` works because every scenario class gets
an automatic ``xdist_group`` marker.

The scenario used here chains two stages (stage 2 consumes stage 1's saved
values), so a split across workers cannot pass by accident.

Those modes still schedule by node id, so what a scenario puts into its stages'
node ids and marks can split a chain too; the tests after the stress test pin
each case.
"""

import re

import pytest

from tests.integration.helpers import stage, write_scenario

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


def _saves_token():
    return stage("login", response=[{"save": {"substitutions": [{"vars": {"token": "t"}}]}}, {"verify": {"status": 200}}])


def _reads_token(name, **fields):
    """A stage that fails unless it runs after `_saves_token`, on the same worker."""
    return stage(name, "/ok?token={{ token }}", **fields)


def _chain(second, **top):
    """Three chained stages, ``second`` in the middle; ``top`` adds scenario keys."""
    return {**top, "stages": [_saves_token(), second, _reads_token("third")]}


# A parametrized stage whose first generated id is "::1".
BY_HOST = _reads_token("by host", parametrize=[{"individual": {"host": ["::1", "localhost"]}}])


@pytest.mark.parametrize(
    ("mode", "scenario", "message"),
    [
        # xdist joins a stage's own group with the scenario's into one name,
        # so the stage became a work unit of its own and ran on another worker.
        pytest.param("loadgroup", _chain(_reads_token("uses db", marks=["xdist_group('db')"])), "*HTTPCHAIN031*", id="stage-xdist-group"),
        # xdist ignores a group whose name has a ']' after its last '@', so
        # every stage of the scenario was a work unit of its own.
        pytest.param("loadgroup", _chain(_reads_token("second"), marks=["xdist_group('db[main]')"]), "*HTTPCHAIN033*", id="bracket-in-declared-group"),
        # loadscope scopes a test by its node id up to the last '::'.
        pytest.param("loadscope", _chain(_reads_token("Users::list")), "*HTTPCHAIN032*", id="separator-in-stage-name"),
        # ... and so does a '::' in a parametrize id; pytest itself handles
        # that id, so only this mode rejects it.
        # The scenario is named by its class's node id.
        pytest.param(
            "loadscope",
            _chain(BY_HOST),
            "*--dist loadscope would run ?'test 1 - by host?::1?'? apart from the rest of scenario 'test_inline.http.json::inline': *",
            id="separator-in-parametrize-id",
        ),
    ],
)
def test_stage_that_would_leave_its_chain_rejected(run_scenario, mode, scenario, message):
    """A stage that a class-preserving mode would still run apart from the rest
    of its scenario fails collection instead of failing on a missing save."""
    result = run_scenario(scenario, args=("-n", "2", "--dist", mode), subprocess=True)
    assert result.parseoutcomes().get("passed", 0) == 0, "no stage may run once a chain would be split"
    assert result.ret != 0
    result.stdout.fnmatch_lines([message])


def test_separator_in_fixture_param_id_rejected_under_loadscope(pytester):
    """A class-scoped fixture's param ids land in every stage's node id too, so
    loadscope rejects a '::' there as well, and the message names the fixture
    as a place to give ids, not only a parametrize step the scenario lacks."""
    pytester.copy_example("conftest.py")
    hosts = pytester.mkdir("hosts")
    hosts.joinpath("conftest.py").write_text('import pytest\n\n\n@pytest.fixture(scope="class", params=["::1", "localhost"])\ndef host(request):\n    return request.param\n')
    write_scenario(hosts, _chain(_reads_token("second"), fixtures=["host"]))
    result = pytester.runpytest_subprocess("-n", "2", "--dist", "loadscope")
    assert result.parseoutcomes().get("passed", 0) == 0, "no stage may run once a chain would be split"
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*these test ids contain '::' (from a stage's parametrize step, or from a fixture's params)*"])


@pytest.mark.parametrize("mode", ["loadfile", "loadgroup"])
def test_separator_in_parametrize_id_runs_outside_loadscope(run_scenario, mode):
    """Only loadscope cuts a node id at its last '::'; the other modes run the chain."""
    result = run_scenario(_chain(BY_HOST), args=("-n", "2", "--dist", mode), subprocess=True)
    result.assert_outcomes(passed=4)


def test_scenarios_sharing_a_declared_xdist_group_run_on_one_worker(pytester, run_scenario):
    """An ``xdist_group`` in the scenario's own marks is the one group its
    stages get, so scenarios declaring the same group run on one worker, as
    they would for any pytest test. The automatic group used to be added on top,
    and xdist joined the two into a name of each scenario's own
    (``db_test_inline.http.json``), so -n 2 ran the scenarios side by side.
    A stage repeating the declared group is in that same group: xdist keeps
    one copy of each name."""
    scenario = {"marks": ["xdist_group('db')"], "stages": [_saves_token(), _reads_token("read")]}
    repeated = {**scenario, "stages": [_saves_token(), _reads_token("read", marks=["xdist_group(name='db')"])]}
    write_scenario(pytester.path, repeated, name="test_other.http.json")
    result = run_scenario(scenario, args=("-n", "2", "--dist", "loadgroup", "-v"), subprocess=True)
    result.assert_outcomes(passed=4)
    workers = re.findall(r"^\[(gw\d+)\] .* PASSED ", result.stdout.str(), re.MULTILINE)
    assert len(workers) == 4
    assert len(set(workers)) == 1, workers


def test_loadgroup_keeps_chain_under_bracketed_directory(pytester):
    """The automatic group is named after the scenario's node id, and xdist
    reads a group back only when no ']' follows the id's last '@': under a
    directory like ``[smoke]`` every stage became a work unit of its own."""
    pytester.copy_example("conftest.py")
    write_scenario(pytester.mkdir("[smoke]"), {"stages": [_saves_token(), _reads_token("second"), _reads_token("third")]})
    result = pytester.runpytest_subprocess("-n", "2", "--dist", "loadgroup")
    result.assert_outcomes(passed=3)
