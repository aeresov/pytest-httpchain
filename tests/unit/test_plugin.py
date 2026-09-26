import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from pytest_httpchain.carrier import Carrier
from pytest_httpchain.constants import ConfigOptions
from pytest_httpchain.plugin import _apply_xdist_group_nodeids, _format_section, _regroup_carrier_items, _sections_will_be_shown, pytest_collect_file, pytest_runtest_makereport


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
        assert pytester.parseconfigure().getini(option) == value

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


def _collect_parent(suffix: str) -> MagicMock:
    parent = MagicMock()
    parent.config.getini.return_value = suffix
    return parent


@pytest.mark.parametrize(
    ("suffix", "filename", "expected_name"),
    [
        ("http", "test_example.http.json", "example"),
        ("http", "test_my_api_test.http.json", "my_api_test"),
        ("api", "test_endpoint.api.json", "endpoint"),
        ("my-test", "test_example.my-test.json", "example"),
        # The suffix is matched literally, regex metacharacters included.
        ("v1.2", "test_example.v1.2.json", "example"),
    ],
)
def test_collect_file_matches(suffix, filename, expected_name):
    parent = _collect_parent(suffix)
    file_path = Path("/some/path") / filename
    with patch("pytest_httpchain.plugin.JsonModule") as json_module:
        assert pytest_collect_file(file_path, parent) is json_module.from_parent.return_value
    json_module.from_parent.assert_called_once_with(parent, path=file_path, name=expected_name)


@pytest.mark.parametrize(
    ("suffix", "filename"),
    [
        ("http", "test_example.api.json"),
        ("http", "example.http.json"),
        ("http", "test_example.http.yaml"),
        ("http", "test_example.json"),
        ("http", "test_example.py"),
        ("http", "test_.http.json"),
        # Unescaped, the '.' in the suffix would match any character.
        ("v1.2", "test_example.v1X2.json"),
    ],
)
def test_collect_file_ignores(suffix, filename):
    assert pytest_collect_file(Path("/some/path") / filename, _collect_parent(suffix)) is None


def test_regroup_pulls_each_scenario_together_and_keeps_other_items():
    """Each scenario class is emitted in stage order at its first item's
    position; a non-scenario test keeps its place between them."""

    class _A(Carrier):
        pass

    class _B(Carrier):
        pass

    class _Item:
        def __init__(self, cls: type | None, stage: int = 0):
            self.cls = cls
            self.function = SimpleNamespace(_httpchain_stage_index=stage)

    a0, a1, b0, plain = _Item(_A, 0), _Item(_A, 1), _Item(_B, 0), _Item(None)
    items: list[Any] = [a1, plain, b0, a0]
    _regroup_carrier_items(items, {it: i for i, it in enumerate(items)})
    assert items == [a0, a1, plain, b0]


def test_xdist_group_nodeid_carried_to_structured_id():
    """From pytest 9.2 ``nodeid`` is derived from a structured ``_id``, so the
    ``@<group>`` suffix an xdist loadgroup worker writes to ``_nodeid`` is carried
    over — for scenario items only, and only where xdist wrote one. Up to 9.1
    ``_nodeid`` is the slot ``nodeid`` reads, and nothing changes."""

    class _Scenario(Carrier):
        pass

    class _NodeId(str):
        @classmethod
        def parse(cls, nodeid: str) -> "_NodeId":
            return cls(nodeid)

    class _Item:  # pytest >= 9.2: nodeid reads _id, _nodeid is a plain attribute
        def __init__(self, cls: type | None, grouped: bool):
            self.cls = cls
            self._id = _NodeId(f"f.json::{cls.__name__ if cls else 'plain'}::t")
            if grouped:  # what an xdist loadgroup worker does
                self._nodeid = f"{self.nodeid}@f.json"

        @property
        def nodeid(self) -> str:
            return str(self._id)

    class _LegacyItem:  # pytest <= 9.1: _nodeid is a slot, there is no _id
        __slots__ = ("__dict__", "_nodeid", "cls")

        def __init__(self):
            self.cls = _Scenario
            self._nodeid = "f.json::_Scenario::t@f.json"

        @property
        def nodeid(self) -> str:
            return self._nodeid

    grouped, ungrouped, plain, legacy = _Item(_Scenario, True), _Item(_Scenario, False), _Item(None, True), _LegacyItem()
    items: list[Any] = [grouped, ungrouped, plain, legacy]
    _apply_xdist_group_nodeids(items)
    assert grouped.nodeid == "f.json::_Scenario::t@f.json"
    assert ungrouped.nodeid == "f.json::_Scenario::t"
    assert plain.nodeid == "f.json::plain::t"  # not a scenario: not the plugin's to touch
    assert legacy.nodeid == "f.json::_Scenario::t@f.json"
    assert not hasattr(legacy, "_id")


def test_format_section_reports_formatter_failure():
    """Reporting must never break the report: a formatter that raises becomes
    the section's text."""

    def broken(_exchange):
        raise ValueError("boom")

    assert _format_section("request", broken, object()) == "<Error formatting request: boom>"


class TestReportSectionsBuiltOnlyWhenShown:
    """Formatting an exchange parses and re-serializes the whole body, and on a
    suite of thousands of passing stages nothing ever prints the result — but
    -rA/-rP do print it, so the guard must not cost those runs their output."""

    @pytest.mark.parametrize(
        ("args", "failed", "expected"),
        [
            pytest.param((), True, True, id="failed"),
            # Default reportchars ('fE') renders no PASSES block at all.
            pytest.param((), False, False, id="passed-default"),
            pytest.param(("-rA",), False, True, id="passed-rA"),
            pytest.param(("-rP",), False, True, id="passed-rP"),
            pytest.param(("-rfEP",), False, True, id="passed-rfEP"),
            # --xfail-tb renders the XFAILURES block, whose reports are 'skipped'.
            pytest.param(("--xfail-tb",), False, True, id="xfail-tb"),
        ],
    )
    def test_sections_will_be_shown(self, pytester, args, failed, expected):
        assert _sections_will_be_shown(pytester.parseconfigure(*args), MagicMock(failed=failed)) is expected

    def test_worker_without_terminal_reporter_formats_everything(self, pytester):
        """An xdist worker unregisters the terminal reporter and ships its
        sections to the controller, which does the rendering: the worker cannot
        tell what will be shown, so it must not drop anything."""
        config = pytester.parseconfigure()
        config.pluginmanager.unregister(name="terminalreporter")
        assert _sections_will_be_shown(config, MagicMock(failed=False))

    @staticmethod
    def _run_hook(config, *, failed: bool) -> list[tuple[str, str]]:
        """Drive the report hook over one recorded exchange, returning the
        sections it attached."""
        request = httpx.Request("GET", "https://example.com/")
        response = httpx.Response(200, json={"a": 1}, request=request)

        class _Scenario(Carrier):
            last_request = request
            last_response = response
            last_exchanges = [(request, response, None)]

        report = MagicMock(failed=failed, skipped=False, sections=[])
        # `cls` is reserved by Mock's own constructor, so it is set afterwards.
        item = MagicMock(config=config, nodeid="t::s")
        item.cls = _Scenario
        hook = pytest_runtest_makereport(item, MagicMock(when="call"))
        hook.send(None)
        with pytest.raises(StopIteration):
            hook.send(report)
        return report.sections

    @pytest.mark.parametrize(
        ("args", "failed", "expected"),
        [
            pytest.param((), False, [], id="passed"),
            pytest.param((), True, ["HTTP Request", "HTTP Response"], id="failed"),
            pytest.param(("-rA",), False, ["HTTP Request", "HTTP Response"], id="passed-with-rA"),
        ],
    )
    def test_hook_formats_only_when_the_sections_will_be_read(self, pytester, args, failed, expected):
        assert [title for title, _ in self._run_hook(pytester.parseconfigure(*args), failed=failed)] == expected

    def test_har_write_failure_is_logged_not_raised(self, pytester, tmp_path, caplog):
        not_a_dir = tmp_path / "file"
        not_a_dir.write_text("")
        config = pytester.parseconfigure("--httpchain-output-dir", str(not_a_dir))

        with caplog.at_level(logging.WARNING, logger="pytest_httpchain.plugin"):
            sections = self._run_hook(config, failed=True)

        assert [title for title, _ in sections] == ["HTTP Request", "HTTP Response"]
        assert [record.getMessage().split(": ", 1)[0] for record in caplog.records] == ["Failed to write HAR file for t::s"]
