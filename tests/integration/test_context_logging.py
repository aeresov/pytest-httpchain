"""Context dumps are DEBUG-only: they serialize every saved value (chained
auth tokens included) and pytest attaches captured logs to failure reports,
so they must be opt-in — and must cost nothing when the level is off."""


def run_with_log_level(pytester, level, example="save/test_save_jmespath.http.json"):
    pytester.copy_example("conftest.py")
    pytester.copy_example(example)
    return pytester.runpytest("-o", "log_cli=true", f"--log-cli-level={level}")


def test_context_dumped_at_debug(pytester):
    result = run_with_log_level(pytester, "DEBUG")
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*context on start*"])


def test_context_not_dumped_at_info(pytester):
    result = run_with_log_level(pytester, "INFO")
    result.assert_outcomes(passed=2)
    result.stdout.no_fnmatch_line("*context on start*")


# A scenario that actually seeds substitutions, so the assertions below are not
# vacuous — the save example has none.
_SEEDING_EXAMPLE = "templates/test_template_expressions.http.json"


def test_seeded_values_not_logged_at_info(pytester):
    """The same boundary, one level down: `process_substitutions` used to log
    every seeded value at INFO — including the result of a token-minting
    function — while the context dumps above were carefully guarded."""
    result = run_with_log_level(pytester, "INFO", _SEEDING_EXAMPLE)
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*Seeded*")
    # The values themselves, not just the log line's shape.
    result.stdout.no_fnmatch_line("*hello world*")


def test_seeded_names_logged_at_debug(pytester):
    """Names still get logged, so the DEBUG trace stays useful."""
    result = run_with_log_level(pytester, "DEBUG", _SEEDING_EXAMPLE)
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*Seeded text*"])
