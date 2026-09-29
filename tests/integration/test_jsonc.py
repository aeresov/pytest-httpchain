"""JSON with comments through a real collected scenario.

The scanner's rules are pinned in tests/unit/jsonref/test_jsonc.py; this is
the end-to-end pin that a ``.jsonc`` scenario is collected, that a ``.json``
one may carry comments too, and that the files a scenario pulls in (a
fragment, a body schema) are read the same way, all the way to the wire.
"""

from tests.integration.helpers import HAR_ARGS

JSONC = ("jsonc/test_commented.http.jsonc", "jsonc/fragments.jsonc", "jsonc/user.schema.jsonc")


def test_jsonc_scenario_runs(run_scenario):
    """Its request comes from a commented fragment, its body is checked
    against a commented schema file, and its first stage's save reaches the
    second, which checks it."""
    run_scenario(*JSONC).assert_outcomes(passed=2)


def test_json_scenario_with_comments_runs(run_scenario):
    run_scenario("jsonc/test_commented.http.json").assert_outcomes(passed=1)


def test_json_and_jsonc_of_the_same_name_are_two_scenarios(run_scenario, pytester):
    """Both are named ``commented`` and both have a stage ``user``: the node
    id holds the file name, extension and all, so the two do not collide.
    Each is collected and runs, and exports its own HAR files, which a name
    built from the scenario and stage names alone would have overwritten."""
    result = run_scenario(*JSONC, "jsonc/test_commented.http.json", args=("-v", *HAR_ARGS))

    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines(
        [
            "test_commented.http.json::commented::test 0 - user PASSED*",
            "test_commented.http.jsonc::commented::test 0 - user PASSED*",
            "test_commented.http.jsonc::commented::test 1 - check PASSED*",
        ],
        consecutive=False,
    )
    assert len(list((pytester.path / "har_out").glob("*.har"))) == 3


def test_a_warning_about_one_of_the_two_says_which(run_scenario):
    """A message that names a scenario gives its node id: the scenario name,
    all it gave before, is the same for both files."""
    result = run_scenario(*JSONC, "jsonc/test_commented.http.json", args=("-k", "check"))

    result.stdout.fnmatch_lines(["*Scenario 'test_commented.http.jsonc::commented': earlier stage(s) [[]'user'[]] were deselected*"])
