import inspect
import logging
import re
import types
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
import pytest_http_engine.loader
import requests
from _pytest import config, nodes, python, reports, runner
from _pytest.config import argparsing
from pydantic import ValidationError
from pytest_http_engine.models import Scenario

SUFFIX: str = "suffix"

logger = logging.Logger(__name__)


class JsonClass(python.Class):
    pass


class JsonModule(python.Module):
    def collect(self) -> Iterable[nodes.Item | nodes.Collector]:
        # load JSON and resolve references
        try:
            test_data: dict[str, Any] = pytest_http_engine.loader.load_json(self.path)
        except pytest_http_engine.loader.LoaderError as e:
            raise nodes.Collector.CollectError("Cannot load JSON file") from e

        # validate models
        try:
            scenario: Scenario = Scenario.model_validate(test_data)
        except ValidationError as e:
            raise nodes.Collector.CollectError("Cannot parse test scenario") from e

        # Carrier class for reusing pytest magic
        class Carrier:
            _data_context: dict[str, Any] = {}
            _http_session: requests.Session | None = None
            _aborted: bool = False

            @classmethod
            def setup_class(cls) -> None:
                cls._http_session = requests.Session()
                # TODO: ssl
                # TODO: auth

            @classmethod
            def teardown_class(cls) -> None:
                cls._data_context.clear()
                if cls._http_session:
                    cls._http_session.close()
                    cls._http_session = None
                cls._aborted = False

            def setup_method(self) -> None:
                pass

            def teardown_method(self) -> None:
                pass

        for i, stage in enumerate(scenario.stages):
            # runner function for scenario's stage
            def _exec_stage(self, **fixture_kwargs: Any):
                logging.info("executing stage")
                for fixture_name, fixture_value in fixture_kwargs.items():
                    logging.info(f"received fixture {fixture_name} = {fixture_value}")

                # Access the class-level HTTP session
                session = self.__class__._http_session
                if session is None:
                    raise RuntimeError("HTTP session not initialized. Ensure setup_class was called.")

                logging.info(f"Using HTTP session: {session} (ID: {id(session)})")
                # Here you would make HTTP requests using the session
                # Example: response = session.get(stage.request.url)

            # inject stage fixtures to request
            _exec_stage.__signature__ = inspect.Signature(
                [inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD)] + [inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in stage.fixtures]
            )

            # decorate in stage markers plus ordering
            for mark in stage.marks + [f"order({i})"]:
                mark_obj = eval(f"pytest.mark.{mark}")
                _exec_stage = mark_obj(_exec_stage)

            # insert in carrier class
            setattr(Carrier, f"test_{stage.name}", _exec_stage)

        dummy_module = types.ModuleType("dummy")
        setattr(dummy_module, self.name, Carrier)
        self._getobj = lambda: dummy_module
        json_class = JsonClass.from_parent(self, path=self.path, name=self.name, obj=Carrier)

        # add markers
        for mark in scenario.marks:
            mark_obj = eval(f"pytest.mark.{mark}")
            json_class.add_marker(mark_obj)
        # in case user added scenario-level fixtures, transform them into "usefixtures" marker
        if scenario.fixtures:
            usefixtures_mark = pytest.mark.usefixtures(*scenario.fixtures)
            json_class.add_marker(usefixtures_mark)

        yield json_class


def pytest_addoption(parser: argparsing.Parser) -> None:
    parser.addini(
        name=SUFFIX,
        help="File suffix for HTTP test files (default: http).",
        type="string",
        default="http",
    )


def pytest_configure(config: config.Config) -> None:
    suffix: str = config.getini(SUFFIX)
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", suffix):
        raise ValueError("suffix must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars")


def _get_test_name_pattern(config: config.Config) -> tuple[re.Pattern[str], str]:
    suffix: str = config.getini(SUFFIX)
    group_name: str = "name"
    return re.compile(rf"^test_(?P<{group_name}>.+)\.{re.escape(suffix)}\.json$"), group_name


def pytest_collect_file(file_path: Path, parent: nodes.Collector) -> JsonClass | None:
    pattern, group_name = _get_test_name_pattern(parent.config)
    match: re.Match[str] | None = pattern.match(file_path.name)
    if match:
        return JsonModule.from_parent(parent, path=file_path, name=match.group(group_name))
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: nodes.Item, call: runner.CallInfo):
    outcome = yield
    report: reports.TestReport = outcome.get_result()
    if call.when == "call":
        report.sections.append(("call_title", "call_value"))
