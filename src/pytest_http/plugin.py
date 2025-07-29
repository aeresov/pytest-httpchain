import inspect
import logging
import re
import types
from collections.abc import Iterable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import pytest_http_engine.loader
import pytest_http_engine.substitution
import requests
from _pytest import config, nodes, python, reports, runner
from _pytest.config import argparsing
from pydantic import ValidationError
from pytest_http_engine.models.entities import FunctionCall, Request, Save, Scenario, Stage, StageCanvas, Verify
from pytest_http_engine.user_function import AuthFunction

import pytest_http.tester

SUFFIX: str = "suffix"

logger = logging.Logger(__name__)


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
            _scenario: Scenario = scenario
            _data_context: dict[str, Any] = {}
            _http_session: requests.Session | None = None
            _aborted: bool = False

            @classmethod
            def setup_class(cls) -> None:
                cls._http_session = requests.Session()
                cls._http_session.verify = cls._scenario.ssl.verify
                if cls._scenario.ssl.cert is not None:
                    cls._http_session.cert = cls._scenario.ssl.cert

                # Configure authentication for the session
                if cls._scenario.auth:
                    resolved_auth = pytest_http_engine.substitution.walk(cls._scenario.auth, cls._data_context)
                    match resolved_auth:
                        case str():
                            auth_instance = AuthFunction.call(resolved_auth)
                        case FunctionCall():
                            auth_instance = AuthFunction.call_with_kwargs(resolved_auth.function, resolved_auth.kwargs)
                    cls._http_session.auth = auth_instance

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

        for i, stage_canvas in enumerate(scenario.stages):
            # Create a closure to capture the current stage
            def make_stage_executor(stage_canvas: StageCanvas):
                def _exec_stage(self, **fixture_kwargs: Any):
                    try:
                        # prepare global data context
                        data_context = deepcopy(self.__class__._data_context)
                        data_context.update(fixture_kwargs)
                        data_context.update(pytest_http_engine.substitution.walk(scenario.vars, data_context))
                        data_context.update(pytest_http_engine.substitution.walk(stage_canvas.vars, data_context))

                        # prepare and validate Stage
                        stage_dict = pytest_http_engine.substitution.walk(stage_canvas.model_dump(), data_context)
                        stage: Stage = Stage.model_validate(stage_dict)

                        # skip if the flow is aborted
                        if self.__class__._aborted and not stage.always_run:
                            pytest.skip(reason="Flow aborted")

                        # make http call
                        request_dict = pytest_http_engine.substitution.walk(stage.request, data_context)
                        request_model: Request = Request.model_validate(request_dict)
                        call_response: requests.Response = pytest_http.tester.call(
                            session=self.__class__._http_session,
                            model=request_model,
                        )

                        # save data from reponse
                        save_dict = pytest_http_engine.substitution.walk(stage.save, data_context)
                        save_model: Save = Save.model_validate(save_dict)
                        context_update: dict[str, Any] = pytest_http.tester.save(
                            response=call_response,
                            model=save_model,
                        )
                        # inject this stage saves
                        data_context.update(context_update)

                        # run verifications
                        verify_dict = pytest_http_engine.substitution.walk(stage.verify, data_context)
                        verify_model: Verify = Verify.model_validate(verify_dict)
                        pytest_http.tester.verify(
                            response=call_response,
                            model=verify_model,
                            context=data_context,
                        )

                        # update carried-on data context
                        self.__class__._data_context.update(context_update)
                    except (pytest_http_engine.substitution.SubstitutionError, ValidationError, pytest_http.tester.TesterError) as e:
                        logger.exception(str(e))
                        self.__class__._aborted = True
                        pytest.fail(reason=str(e), pytrace=False)

                return _exec_stage

            # Create the stage executor function
            stage_executor = make_stage_executor(stage_canvas)

            # inject stage fixtures to request
            all_fixtures = ["self"] + stage_canvas.fixtures + scenario.fixtures
            stage_executor.__signature__ = inspect.Signature([inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in all_fixtures])

            # decorate in stage markers plus ordering
            for mark in stage_canvas.marks + [f"order({i})"]:
                mark_obj = eval(f"pytest.mark.{mark}")
                stage_executor = mark_obj(stage_executor)

            # insert in carrier class
            setattr(Carrier, f"test_{stage_canvas.name}", stage_executor)

        dummy_module = types.ModuleType("dummy")
        setattr(dummy_module, self.name, Carrier)
        self._getobj = lambda: dummy_module
        json_class = python.Class.from_parent(self, path=self.path, name=self.name, obj=Carrier)

        # add markers
        for mark in scenario.marks:
            mark_obj = eval(f"pytest.mark.{mark}")
            json_class.add_marker(mark_obj)

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


def pytest_collect_file(file_path: Path, parent: nodes.Collector) -> nodes.Collector | None:
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
