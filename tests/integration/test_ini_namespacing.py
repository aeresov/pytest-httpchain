"""Namespaced ini options (httpchain_*) with deprecated pre-0.10 aliases.

pytest ini options live in one global namespace shared by every plugin, so the
options carry an ``httpchain_`` prefix as of 0.10. The old un-prefixed names
keep working through the 0.10 series with a config-time deprecation warning;
when both spellings are set, the prefixed one wins.
"""

import json

SCENARIO = json.dumps(
    {
        "stages": [
            {
                "name": "only",
                "fixtures": ["server"],
                "request": {"url": "{{ server }}/ok"},
                "response": [{"verify": {"status": 200}}],
            }
        ]
    }
)


def _setup(pytester, suffix: str):
    pytester.copy_example("conftest.py")
    (pytester.path / f"test_one.{suffix}.json").write_text(SCENARIO)


def test_namespaced_suffix_works(pytester):
    _setup(pytester, "chain")
    pytester.makeini("[pytest]\nhttpchain_suffix = chain\n")
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*PytestDeprecationWarning*")


def test_legacy_suffix_still_works_with_deprecation_warning(pytester):
    _setup(pytester, "chain")
    pytester.makeini("[pytest]\nsuffix = chain\n")
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*'suffix' is deprecated, use 'httpchain_suffix'*"])


def test_namespaced_wins_when_both_set(pytester):
    # legacy says "old", namespaced says "chain": only the "chain" file collects
    _setup(pytester, "chain")
    (pytester.path / "test_two.old.json").write_text(SCENARIO)
    pytester.makeini("[pytest]\nsuffix = old\nhttpchain_suffix = chain\n")
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*'suffix' is deprecated and ignored because 'httpchain_suffix' is also set*"])


def test_namespaced_option_via_override_ini(pytester):
    """-o httpchain_suffix=... must be honored (config.inicfg alone misses -o
    values on pytest 8.x, so explicit-set detection also scans --override-ini)."""
    _setup(pytester, "chain")
    result = pytester.runpytest("-s", "-o", "httpchain_suffix=chain")
    result.assert_outcomes(passed=1)


def test_legacy_option_via_override_ini(pytester):
    """-o suffix=... (the legacy spelling) keeps working through 0.10."""
    _setup(pytester, "chain")
    result = pytester.runpytest("-s", "-o", "suffix=chain")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*'suffix' is deprecated, use 'httpchain_suffix'*"])


def test_override_ini_beats_ini_file(pytester):
    """pytest's documented precedence: -o overrides the ini file value."""
    _setup(pytester, "chain")
    pytester.makeini("[pytest]\nsuffix = old\n")
    result = pytester.runpytest("-s", "-o", "httpchain_suffix=chain")
    result.assert_outcomes(passed=1)


def test_legacy_output_dir_flag_in_addopts_warns(pytester):
    """The deprecation warning must also fire when the flag is pinned in ini
    addopts (the common place to persist it), not just argv."""
    _setup(pytester, "http")
    pytester.makeini("[pytest]\naddopts = --output-dir=har_out\n")
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*'--output-dir' is deprecated*"])
    assert any((pytester.path / "har_out").glob("*.har"))


def test_legacy_output_dir_flag_warns(pytester):
    _setup(pytester, "http")
    result = pytester.runpytest("-s", "--output-dir=har_out")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*'--output-dir' is deprecated, use '--httpchain-output-dir'*"])
    assert any((pytester.path / "har_out").glob("*.har"))


def test_namespaced_output_dir_flag(pytester):
    _setup(pytester, "http")
    result = pytester.runpytest("-s", "--httpchain-output-dir=har_out")
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*deprecated*")
    assert any((pytester.path / "har_out").glob("*.har"))
