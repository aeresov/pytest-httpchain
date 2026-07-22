"""Context dumps are DEBUG-only: they serialize every saved value (chained
auth tokens included) and pytest attaches captured logs to failure reports,
so they must be opt-in — and must cost nothing when the level is off."""


def run_with_log_level(pytester, level):
    pytester.copy_example("conftest.py")
    pytester.copy_example("save/test_save_jmespath.http.json")
    return pytester.runpytest("-o", "log_cli=true", f"--log-cli-level={level}")


def test_context_dumped_at_debug(pytester):
    result = run_with_log_level(pytester, "DEBUG")
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*context on start*"])


def test_context_not_dumped_at_info(pytester):
    result = run_with_log_level(pytester, "INFO")
    result.assert_outcomes(passed=2)
    result.stdout.no_fnmatch_line("*context on start*")
