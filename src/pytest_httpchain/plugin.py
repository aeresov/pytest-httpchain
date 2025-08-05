import inspect
import logging
import re
import types
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import pytest_httpchain_engine.loader
import pytest_httpchain_engine.models.entities
from _pytest import config, nodes, python, reports, runner
from _pytest.config import argparsing
from pydantic import ValidationError
from pytest_httpchain_engine.models.entities import Scenario, Stage

from pytest_httpchain.core.executor import StageExecutor
from pytest_httpchain.core.session import HTTPSessionManager

logger = logging.getLogger(__name__)

SUFFIX: str = "suffix"
REF_PARENT_TRAVERSAL_DEPTH: str = "ref_parent_traversal_depth"


class StageCarrier:
    """Base class for carrying scenario stages through pytest execution."""

    _scenario: Scenario
    _session_manager: HTTPSessionManager
    _stage_executor: StageExecutor


class JsonModule(python.Module):
    """JSON test module that collects and executes HTTP chain tests."""

    def collect(self) -> Iterable[nodes.Item | nodes.Collector]:
        """Collect test items from a JSON module."""
        scenario = self._load_scenario()
        carrier_class = self._create_carrier_class(scenario)
        self._add_stage_methods(carrier_class, scenario)
        yield self._create_test_class(carrier_class, scenario)

    def _load_scenario(self) -> Scenario:
        """Load and validate the test scenario from JSON."""
        ref_parent_traversal_depth = int(self.config.getini("ref_parent_traversal_depth"))

        try:
            test_data = pytest_httpchain_engine.loader.load_json(
                self.path,
                max_parent_traversal_depth=ref_parent_traversal_depth,
            )
        except pytest_httpchain_engine.loader.LoaderError as e:
            raise nodes.Collector.CollectError("Cannot load JSON file") from e

        try:
            return Scenario.model_validate(test_data)
        except ValidationError as e:
            raise nodes.Collector.CollectError("Cannot parse test scenario") from e

    def _create_carrier_class(self, scenario: Scenario) -> type[StageCarrier]:
        """Create a carrier class for the scenario."""

        class Carrier(StageCarrier):
            _scenario = scenario
            _session_manager = HTTPSessionManager(scenario)
            _stage_executor = StageExecutor(_session_manager, scenario)

            @classmethod
            def setup_class(cls) -> None:
                cls._session_manager.setup()

            @classmethod
            def teardown_class(cls) -> None:
                cls._session_manager.teardown()
                cls._stage_executor._aborted = False

            def setup_method(self) -> None:
                pass

            def teardown_method(self) -> None:
                pass

        return Carrier

    def _add_stage_methods(self, carrier_class: type[StageCarrier], scenario: Scenario) -> None:
        """Add test methods for each stage to the carrier class."""
        for i, stage_canvas in enumerate(scenario.stages):
            stage_method = self._create_stage_method(stage_canvas)

            all_fixtures = ["self"] + stage_canvas.fixtures + scenario.fixtures
            stage_method.__signature__ = inspect.Signature([inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in all_fixtures])

            stage_method = self._apply_markers(stage_method, stage_canvas, i)
            setattr(carrier_class, f"test_{i}_{stage_canvas.name}", stage_method)

    def _create_stage_method(self, stage_template: Stage):
        """Create a test method for a stage."""

        def stage_method(self, **fixture_kwargs: Any):
            self.__class__._stage_executor.execute_stage(stage_template, fixture_kwargs)

        return stage_method

    def _apply_markers(self, method, stage: Stage, index: int):
        """Apply pytest markers to a stage method."""
        markers = stage.marks + [f"order({index})"]

        for mark_str in markers:
            marker = self._create_marker_from_string(mark_str)
            if marker:
                method = marker(method)

        return method

    def _create_marker_from_string(self, mark_str: str):
        """Create a pytest marker from a string using eval."""
        try:
            return eval(f"pytest.mark.{mark_str}")
        except Exception as e:
            logger.warning(f"Failed to create marker '{mark_str}': {e}")
            return None

    def _create_test_class(self, carrier_class: type[StageCarrier], scenario: Scenario) -> python.Class:
        """Create a pytest Class node for the carrier."""
        dummy_module = types.ModuleType("dummy")
        setattr(dummy_module, self.name, carrier_class)
        self._getobj = lambda: dummy_module

        json_class = python.Class.from_parent(
            self,
            path=self.path,
            name=self.name,
            obj=carrier_class,
        )

        for mark_str in scenario.marks:
            marker = self._create_marker_from_string(mark_str)
            if marker:
                json_class.add_marker(marker)

        return json_class


def pytest_addoption(parser: argparsing.Parser) -> None:
    """Add command-line options for the plugin."""
    parser.addini(
        name=SUFFIX,
        help="File suffix for HTTP test files.",
        type="string",
        default="http",
    )
    parser.addini(
        name=REF_PARENT_TRAVERSAL_DEPTH,
        help="Maximum number of parent directory traversals allowed in $ref paths.",
        type="string",
        default="3",
    )


def pytest_configure(config: config.Config) -> None:
    """Validate configuration settings."""
    suffix: str = config.getini(SUFFIX)
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", suffix):
        raise ValueError("suffix must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars")

    try:
        ref_parent_traversal_depth = int(config.getini(REF_PARENT_TRAVERSAL_DEPTH))
        if ref_parent_traversal_depth < 0:
            raise ValueError("Maximum number of parent directory traversals must be non-negative")
    except ValueError as e:
        raise ValueError("Maximum number of parent directory traversals must be a non-negative integer") from e


def pytest_collect_file(file_path: Path, parent: nodes.Collector) -> nodes.Collector | None:
    """Collect JSON test files matching the configured pattern."""
    pattern, group_name = _get_test_name_pattern(parent.config)
    match: re.Match[str] | None = pattern.match(file_path.name)
    if match:
        return JsonModule.from_parent(parent, path=file_path, name=match.group(group_name))
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: nodes.Item, call: runner.CallInfo):
    """Add custom sections to test reports."""
    outcome = yield
    report: reports.TestReport = outcome.get_result()
    if call.when == "call":
        report.sections.append(("call_title", "call_value"))


def _get_test_name_pattern(config: config.Config) -> tuple[re.Pattern[str], str]:
    """Get the regex pattern for matching test file names."""
    suffix: str = config.getini(SUFFIX)
    group_name: str = "name"
    return re.compile(rf"^test_(?P<{group_name}>.+)\.{re.escape(suffix)}\.json$"), group_name
