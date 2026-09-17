"""Context dumps are DEBUG-only: they serialize every saved value (chained
auth tokens included) and pytest attaches captured logs to failure reports,
so they must be opt-in — and must cost nothing when the level is off."""


def run_with_log_level(run_scenario, level, example="save/test_save_jmespath.http.json"):
    return run_scenario(example, args=("-o", "log_cli=true", f"--log-cli-level={level}"))


def test_context_dumped_at_debug(run_scenario):
    result = run_with_log_level(run_scenario, "DEBUG")
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*context on start*"])


def test_context_not_dumped_at_info(run_scenario):
    result = run_with_log_level(run_scenario, "INFO")
    result.assert_outcomes(passed=2)
    result.stdout.no_fnmatch_line("*context on start*")


# A scenario that actually seeds substitutions, so the assertions below are not
# vacuous — the save example has none.
_SEEDING_EXAMPLE = "templates/test_template_expressions.http.json"


# These test names deliberately avoid the word this file greps for. `fnmatch` is
# CASE-INSENSITIVE on Windows (it normalizes case via os.path.normcase), and
# pytester prints a `rootdir:` line containing a temp directory named after the
# running test — so a test called `..._seeded_...` makes `no_fnmatch_line`
# ("*Seeded*") match the harness's own output and fail on Windows only.


def test_substitution_values_not_logged_at_info(run_scenario):
    """The same boundary, one level down: `process_substitutions` used to log
    every seeded value at INFO — including the result of a token-minting
    function — while the context dumps above were carefully guarded."""
    result = run_with_log_level(run_scenario, "INFO", _SEEDING_EXAMPLE)
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*Seeded*")
    # The values themselves, not just the log line's shape.
    result.stdout.no_fnmatch_line("*hello world*")


def test_substitution_names_logged_at_debug(run_scenario):
    """Names still get logged, so the DEBUG trace stays useful."""
    result = run_with_log_level(run_scenario, "DEBUG", _SEEDING_EXAMPLE)
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*Seeded text*"])
