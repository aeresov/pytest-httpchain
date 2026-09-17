from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from pytest_httpchain.carrier import Carrier
from pytest_httpchain.constants import ConfigOptions
from pytest_httpchain.plugin import _sections_will_be_shown, pytest_collect_file, pytest_runtest_makereport


class TestPytestConfigure:
    """Configuration validation through REAL pytest machinery.

    ``pytester.parseconfigure`` builds a genuine Config from an ini file, so
    option registration, pytest's type="int" coercion (whose bare ValueError
    the plugin must wrap into a clean UsageError), and the range checks are
    exercised end to end instead of emulated on a mock."""

    def test_defaults_configure_cleanly(self, pytester):
        pytester.parseconfigure()

    @pytest.mark.parametrize(
        ("option", "value"),
        [
            pytest.param(ConfigOptions.SUFFIX, "mytest123", id="suffix-alphanumeric"),
            pytest.param(ConfigOptions.SUFFIX, "my_test", id="suffix-underscore"),
            pytest.param(ConfigOptions.SUFFIX, "my-test", id="suffix-hyphen"),
            pytest.param(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, 0, id="ref-depth-zero"),
            pytest.param(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, 10, id="ref-depth-positive"),
            pytest.param(ConfigOptions.MAX_COMPREHENSION_LENGTH, 1, id="max-comp-minimum"),
            pytest.param(ConfigOptions.MAX_COMPREHENSION_LENGTH, 1000000, id="max-comp-maximum"),
            pytest.param(ConfigOptions.MAX_PARALLEL_ITERATIONS, 1, id="max-parallel-minimum"),
            pytest.param(ConfigOptions.MAX_PARALLEL_ITERATIONS, 1000000, id="max-parallel-maximum"),
        ],
    )
    def test_valid_config(self, pytester, option, value):
        pytester.makeini(f"[pytest]\n{option} = {value}\n")
        # Should not raise.
        pytester.parseconfigure()

    def test_comprehension_cap_restored_on_unconfigure(self, pytester):
        """The cap is a process-wide simpleeval global: an in-process pytester
        run (or any nested session) must put back what it found, or its value
        leaks into the enclosing process's template engine (M-review)."""
        import simpleeval

        before = simpleeval.MAX_COMPREHENSION_LENGTH
        pytester.makeini(f"[pytest]\n{ConfigOptions.MAX_COMPREHENSION_LENGTH} = 7\n")
        config = pytester.parseconfigure()
        assert simpleeval.MAX_COMPREHENSION_LENGTH == 7

        config._ensure_unconfigure()
        assert simpleeval.MAX_COMPREHENSION_LENGTH == before

    @pytest.mark.parametrize(
        ("option", "value", "match"),
        [
            pytest.param(ConfigOptions.SUFFIX, "test.http", "suffix must contain only alphanumeric", id="suffix-special-chars"),
            pytest.param(ConfigOptions.SUFFIX, "test http", "suffix must contain only alphanumeric", id="suffix-spaces"),
            pytest.param(ConfigOptions.SUFFIX, "a" * 33, "suffix must contain only alphanumeric", id="suffix-too-long"),
            pytest.param(ConfigOptions.SUFFIX, "", "suffix must contain only alphanumeric", id="suffix-empty"),
            pytest.param(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, -1, "must be non-negative", id="ref-depth-negative"),
            pytest.param(ConfigOptions.MAX_COMPREHENSION_LENGTH, 0, "must be a positive integer", id="max-comp-zero"),
            pytest.param(ConfigOptions.MAX_COMPREHENSION_LENGTH, -1, "must be a positive integer", id="max-comp-negative"),
            pytest.param(ConfigOptions.MAX_COMPREHENSION_LENGTH, 1000001, "must not exceed 1,000,000", id="max-comp-too-large"),
            pytest.param(ConfigOptions.MAX_PARALLEL_ITERATIONS, 0, "must be a positive integer", id="max-parallel-zero"),
            pytest.param(ConfigOptions.MAX_PARALLEL_ITERATIONS, -1, "must be a positive integer", id="max-parallel-negative"),
            pytest.param(ConfigOptions.MAX_PARALLEL_ITERATIONS, 1000001, "must not exceed 1,000,000", id="max-parallel-too-large"),
            # A non-integer must surface as a clean UsageError rather than an
            # INTERNALERROR traceback, but the wording depends on who wraps it.
            # Through pytest 9 the coercion is a bare int(value) whose ValueError
            # the plugin catches and re-raises as "<option> must be an integer:
            # <ValueError>"; pytest 10 raises the UsageError itself and the
            # plugin's handler never runs, leaving the bare ValueError text. Both
            # embed int()'s own message, so match on that and let either wrapper
            # win — the type assertion below is what actually pins the behavior.
            pytest.param(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH, "notanumber", "invalid literal for int", id="ref-depth-non-integer"),
            pytest.param(ConfigOptions.MAX_COMPREHENSION_LENGTH, "notanumber", "invalid literal for int", id="max-comp-non-integer"),
            pytest.param(ConfigOptions.MAX_PARALLEL_ITERATIONS, "notanumber", "invalid literal for int", id="max-parallel-non-integer"),
        ],
    )
    def test_invalid_config(self, pytester, option, value, match):
        pytester.makeini(f"[pytest]\n{option} = {value}\n")
        with pytest.raises(pytest.UsageError, match=match):
            pytester.parseconfigure()


class TestPytestCollectFile:
    def make_parent(self, suffix="http"):
        parent = MagicMock()
        parent.config.getini.return_value = suffix
        return parent

    def test_matches_standard_pattern(self):
        parent = self.make_parent()
        file_path = Path("/some/path/test_example.http.json")

        with patch("pytest_httpchain.plugin.JsonModule") as MockJsonModule:
            MockJsonModule.from_parent.return_value = "mock_module"
            result = pytest_collect_file(file_path, parent)

            assert result == "mock_module"
            MockJsonModule.from_parent.assert_called_once()
            call_kwargs = MockJsonModule.from_parent.call_args[1]
            assert call_kwargs["name"] == "example"

    def test_matches_underscore_in_name(self):
        parent = self.make_parent()
        file_path = Path("/some/path/test_my_api_test.http.json")

        with patch("pytest_httpchain.plugin.JsonModule") as MockJsonModule:
            MockJsonModule.from_parent.return_value = "mock_module"
            result = pytest_collect_file(file_path, parent)

            assert result == "mock_module"
            call_kwargs = MockJsonModule.from_parent.call_args[1]
            assert call_kwargs["name"] == "my_api_test"

    def test_matches_custom_suffix(self):
        parent = self.make_parent(suffix="api")
        file_path = Path("/some/path/test_endpoint.api.json")

        with patch("pytest_httpchain.plugin.JsonModule") as MockJsonModule:
            MockJsonModule.from_parent.return_value = "mock_module"
            result = pytest_collect_file(file_path, parent)

            assert result == "mock_module"

    def test_does_not_match_wrong_suffix(self):
        parent = self.make_parent(suffix="http")
        file_path = Path("/some/path/test_example.api.json")

        result = pytest_collect_file(file_path, parent)

        assert result is None

    def test_does_not_match_missing_test_prefix(self):
        parent = self.make_parent()
        file_path = Path("/some/path/example.http.json")

        result = pytest_collect_file(file_path, parent)

        assert result is None

    def test_does_not_match_missing_json_extension(self):
        parent = self.make_parent()
        file_path = Path("/some/path/test_example.http.yaml")

        result = pytest_collect_file(file_path, parent)

        assert result is None

    def test_does_not_match_regular_json(self):
        parent = self.make_parent()
        file_path = Path("/some/path/test_example.json")

        result = pytest_collect_file(file_path, parent)

        assert result is None

    def test_does_not_match_python_file(self):
        parent = self.make_parent()
        file_path = Path("/some/path/test_example.py")

        result = pytest_collect_file(file_path, parent)

        assert result is None

    def test_does_not_match_partial_pattern(self):
        parent = self.make_parent()
        file_path = Path("/some/path/test_.http.json")  # empty name

        result = pytest_collect_file(file_path, parent)

        assert result is None

    def test_suffix_with_hyphen(self):
        parent = self.make_parent(suffix="my-test")
        file_path = Path("/some/path/test_example.my-test.json")

        with patch("pytest_httpchain.plugin.JsonModule") as MockJsonModule:
            MockJsonModule.from_parent.return_value = "mock_module"
            result = pytest_collect_file(file_path, parent)

            assert result == "mock_module"

    def test_suffix_special_chars_escaped(self):
        # A suffix containing a regex metacharacter ('.') must be matched
        # literally. pytest_collect_file re.escape()s the suffix, so the '.' only
        # matches a literal dot — not any character.
        parent = self.make_parent(suffix="v1.2")

        # Literal match: the dot in the suffix lines up with the dot in the name.
        literal = Path("/some/path/test_example.v1.2.json")
        with patch("pytest_httpchain.plugin.JsonModule") as MockJsonModule:
            MockJsonModule.from_parent.return_value = "mock_module"
            assert pytest_collect_file(literal, parent) == "mock_module"
            assert MockJsonModule.from_parent.call_args[1]["name"] == "example"

        # Without escaping, '.' would match any char, so 'v1X2' would match too.
        # With escaping it must NOT, proving the metacharacter is treated literally.
        injected = Path("/some/path/test_example.v1X2.json")
        assert pytest_collect_file(injected, parent) is None


class TestReportSectionsBuiltOnlyWhenShown:
    """Formatting an exchange parses and re-serializes the whole body, and on a
    suite of thousands of passing stages nothing ever prints the result — but
    -rA/-rP do print it, so the guard must not cost those runs their output."""

    @staticmethod
    def _report(*, failed: bool) -> MagicMock:
        return MagicMock(failed=failed)

    def test_failed_report_is_formatted(self, pytester):
        assert _sections_will_be_shown(pytester.parseconfigure(), self._report(failed=True))

    def test_passing_report_is_skipped_by_default(self, pytester):
        """Default reportchars ('fE') renders no PASSES block at all."""
        assert not _sections_will_be_shown(pytester.parseconfigure(), self._report(failed=False))

    @pytest.mark.parametrize("flag", ["-rA", "-rP", "-rfEP"])
    def test_passing_report_is_formatted_when_asked_for(self, pytester, flag):
        assert _sections_will_be_shown(pytester.parseconfigure(flag), self._report(failed=False))

    def test_xfail_tb_formats_non_failing_reports(self, pytester):
        """--xfail-tb renders the XFAILURES block, whose reports are 'skipped'."""
        assert _sections_will_be_shown(pytester.parseconfigure("--xfail-tb"), self._report(failed=False))

    def test_worker_without_terminal_reporter_formats_everything(self, pytester):
        """An xdist worker unregisters the terminal reporter and ships its
        sections to the controller, which does the rendering: the worker cannot
        tell what will be shown, so it must not drop anything."""
        config = pytester.parseconfigure()
        config.pluginmanager.unregister(name="terminalreporter")
        assert _sections_will_be_shown(config, self._report(failed=False))

    @staticmethod
    def _run_hook(config, *, failed: bool) -> list[tuple[str, str]]:
        """Drive the report hook over one recorded exchange, returning the
        sections it attached."""
        request = httpx.Request("GET", "https://example.com/")
        response = httpx.Response(200, json={"a": 1}, request=request)

        class _Scenario(Carrier):
            last_request = request
            last_response = response

        report = MagicMock(failed=failed, skipped=False, sections=[])
        # `cls` is reserved by Mock's own constructor, so it is set afterwards.
        item = MagicMock(config=config, nodeid="t::s")
        item.cls = _Scenario
        hook = pytest_runtest_makereport(item, MagicMock(when="call"))
        hook.send(None)
        with pytest.raises(StopIteration):
            hook.send(report)
        return report.sections

    def test_hook_skips_formatting_for_a_passing_stage(self, pytester):
        assert self._run_hook(pytester.parseconfigure(), failed=False) == []

    @pytest.mark.parametrize("failed", [True, False], ids=["failed", "passed-with-rA"])
    def test_hook_formats_when_the_sections_will_be_read(self, pytester, failed):
        config = pytester.parseconfigure() if failed else pytester.parseconfigure("-rA")
        assert [title for title, _ in self._run_hook(config, failed=failed)] == ["HTTP Request", "HTTP Response"]
