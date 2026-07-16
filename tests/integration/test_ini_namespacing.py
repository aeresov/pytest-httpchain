"""Namespaced ini options (httpchain_*).

pytest ini options live in one global namespace shared by every plugin, so the
options carry an ``httpchain_`` prefix as of 0.10. The old un-prefixed names
were deprecated through the 0.10 series and REMOVED in 0.11: pytest no longer
knows them, so an ini file setting `suffix` gets the standard unknown-option
treatment and `--output-dir` is an unrecognized argument.
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


def test_removed_legacy_suffix_has_no_effect(pytester):
    """The pre-0.10 `suffix` spelling is gone: it no longer changes discovery
    (the .chain file is not collected), and pytest reports it as an unknown
    config option."""
    _setup(pytester, "chain")
    pytester.makeini("[pytest]\nsuffix = chain\n")
    result = pytester.runpytest("-s")
    result.assert_outcomes(passed=0)
    result.stdout.fnmatch_lines(["*Unknown config option: suffix*"])


def test_namespaced_option_via_override_ini(pytester):
    _setup(pytester, "chain")
    result = pytester.runpytest("-s", "-o", "httpchain_suffix=chain")
    result.assert_outcomes(passed=1)


def test_override_ini_beats_ini_file(pytester):
    """pytest's documented precedence: -o overrides the ini file value."""
    _setup(pytester, "chain")
    pytester.makeini("[pytest]\nhttpchain_suffix = old\n")
    result = pytester.runpytest("-s", "-o", "httpchain_suffix=chain")
    result.assert_outcomes(passed=1)


def test_removed_output_dir_flag_is_rejected(pytester):
    """The pre-0.10 `--output-dir` alias is gone: pytest rejects it as an
    unrecognized argument instead of silently writing HAR files."""
    _setup(pytester, "http")
    result = pytester.runpytest("-s", "--output-dir=har_out")
    assert result.ret != 0
    result.stderr.fnmatch_lines(["*unrecognized arguments*--output-dir*"])
    assert not (pytester.path / "har_out").exists()


def test_namespaced_output_dir_flag(pytester):
    _setup(pytester, "http")
    result = pytester.runpytest("-s", "--httpchain-output-dir=har_out")
    result.assert_outcomes(passed=1)
    result.stdout.no_fnmatch_line("*deprecated*")
    assert any((pytester.path / "har_out").glob("*.har"))
