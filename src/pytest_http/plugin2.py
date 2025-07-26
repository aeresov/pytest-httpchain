import logging
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from _pytest import config, nodes, python, reports, runner
from _pytest.config import argparsing

SUFFIX: str = "suffix"

logger = logging.Logger(__name__)


class Stooge:
    @classmethod
    def setup_class(cls) -> None:
        pass

    @classmethod
    def teardown_class(cls) -> None:
        pass

    def setup_method(self) -> None:
        pass

    def teardown_method(self) -> None:
        pass


class Unprocessable(pytest.Item):
    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.message: str = message

    def runtest(self) -> None:
        pytest.skip(reason=self.message)

    def reportinfo(self) -> tuple[Path, int, str]:
        return self.path, None, self.name


class JsonFile(python.Class):
    pass


class JsonModule(python.Module):
    def collect(self) -> Iterable[nodes.Item | nodes.Collector]:
        yield JsonFile.from_parent(self, path=self.path, name=self.name)
        # dummy_module = types.ModuleType("dummy")

        # # Add a dynamic test method to Victim class
        # def test_dynamic(self, **fixture_kwargs: Any):
        #     for fixture_name, fixture_value in fixture_kwargs.items():
        #         logging.info(f"received fixture {fixture_name} = {fixture_value}")
        #     assert False

        # # Dynamically add the test method to Victim
        # Stooge.test_dynamic = test_dynamic

        # # Dynamically modify the method signature directly using the JSONStage approach
        # fixtures_to_inject = ["string_value", "int_value"]
        # Stooge.test_dynamic.__signature__ = inspect.Signature(
        #     [inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD)] +
        #     [inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in fixtures_to_inject]
        # )

        # # Dynamically add an arbitrary mark to the test method
        # arbitrary_mark = "slow"  # This could be any string
        # mark_obj = eval(f"pytest.mark.{arbitrary_mark}")
        # Stooge.test_dynamic = mark_obj(Stooge.test_dynamic)

        # setattr(dummy_module, self.name, Stooge)
        # self._getobj = lambda: dummy_module
        # json_file = JsonFile.from_parent(self, path=self.path, name=self.name, obj=Stooge)
        # for mark in ["usefixtures('string_value')", "xfail"]:
        #     mark_obj = eval(f"pytest.mark.{mark}")
        #     json_file.add_marker(mark_obj)
        # yield json_file

        def _unprocessable(self, message: str) -> Unprocessable:
            return Unprocessable.from_parent(self, name=self.name, message=message)


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


def pytest_collect_file(file_path: Path, parent: nodes.Collector) -> JsonFile | None:
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
