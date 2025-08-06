import inspect
import json
import logging
import re
import types
from collections.abc import Iterable
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from typing import Any

import jmespath
import jsonschema
import pytest
import pytest_httpchain_engine.loader
import pytest_httpchain_engine.models.entities
import pytest_httpchain_engine.substitution
import requests
from _pytest import config, nodes, python, reports, runner
from _pytest.config import argparsing
from pydantic import ValidationError
from pytest_httpchain_engine.exceptions import HTTPChainError
from pytest_httpchain_engine.functions import AuthFunction, VerificationFunction
from pytest_httpchain_engine.models.entities import (
    Request,
    Save,
    Scenario,
    Stage,
    UserFunctionKwargs,
    UserFunctionName,
    Verify,
)
from pytest_httpchain_engine.models.types import check_json_schema
from simpleeval import EvalWithCompoundTypes

from pytest_httpchain.constants import ConfigOptions

logger = logging.getLogger(__name__)


# Exception classes
class RequestError(HTTPChainError):
    """An error making HTTP request."""


class ResponseError(HTTPChainError):
    """An error processing HTTP response."""


class VerificationError(HTTPChainError):
    """An error during response verification."""


class JsonModule(python.Module):
    """JSON test module that collects and executes HTTP chain tests."""

    def collect(self) -> Iterable[nodes.Item | nodes.Collector]:
        """Collect test items from a JSON module."""
        # Load and validate the test scenario from JSON
        ref_parent_traversal_depth = int(self.config.getini(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH))

        try:
            test_data = pytest_httpchain_engine.loader.load_json(
                self.path,
                max_parent_traversal_depth=ref_parent_traversal_depth,
            )
        except pytest_httpchain_engine.loader.LoaderError as e:
            raise nodes.Collector.CollectError("Cannot load JSON file") from e

        try:
            scenario = Scenario.model_validate(test_data)
        except ValidationError as e:
            raise nodes.Collector.CollectError("Cannot parse test scenario") from e

        # Create carrier class with all integrated functionality
        class Carrier:
            """Carrier class that integrates all test execution functionality."""

            _scenario = scenario
            _session: requests.Session | None = None
            _data_context: dict[str, Any] = {}
            _aborted = False

            @classmethod
            def setup_class(cls) -> None:
                """Initialize the HTTP session and data context."""
                cls._data_context = {}
                cls._session = requests.Session()

                # Configure SSL settings
                cls._session.verify = cls._scenario.ssl.verify
                if cls._scenario.ssl.cert is not None:
                    cls._session.cert = cls._scenario.ssl.cert

                # Configure authentication
                if cls._scenario.auth:
                    resolved_auth = pytest_httpchain_engine.substitution.walk(cls._scenario.auth, cls._data_context)

                    match resolved_auth:
                        case str():
                            auth_instance = AuthFunction.call(resolved_auth)
                        case UserFunctionKwargs():
                            auth_instance = AuthFunction.call_with_kwargs(resolved_auth.function, resolved_auth.kwargs)

                    cls._session.auth = auth_instance

            @classmethod
            def teardown_class(cls) -> None:
                """Clean up the HTTP session and reset state."""
                if cls._session:
                    cls._session.close()
                    cls._session = None
                cls._data_context.clear()
                cls._aborted = False

            def setup_method(self) -> None:
                pass

            def teardown_method(self) -> None:
                pass

            @classmethod
            def execute_stage(cls, stage_template: Stage, fixture_kwargs: dict[str, Any]) -> None:
                """Execute a single test stage."""
                try:
                    # Prepare data context
                    data_context = deepcopy(cls._data_context)
                    data_context.update(fixture_kwargs)
                    data_context.update(pytest_httpchain_engine.substitution.walk(cls._scenario.vars, data_context))
                    data_context.update(pytest_httpchain_engine.substitution.walk(stage_template.vars, data_context))

                    # Prepare and validate Stage
                    stage = pytest_httpchain_engine.substitution.walk(stage_template, data_context)

                    # Skip if the flow is aborted
                    if cls._aborted and not stage.always_run:
                        pytest.skip(reason="Flow aborted")

                    # Make HTTP call
                    if cls._session is None:
                        raise RuntimeError("Session not initialized")

                    request_dict = pytest_httpchain_engine.substitution.walk(stage.request, data_context)
                    request_model = Request.model_validate(request_dict)

                    # Prepare request parameters directly
                    request_params: dict[str, Any] = {
                        "timeout": request_model.timeout,
                        "allow_redirects": request_model.allow_redirects,
                    }

                    if request_model.params:
                        request_params["params"] = request_model.params
                    if request_model.headers:
                        request_params["headers"] = request_model.headers

                    if request_model.ssl:
                        if request_model.ssl.verify is not None:
                            request_params["verify"] = request_model.ssl.verify
                        if request_model.ssl.cert is not None:
                            request_params["cert"] = request_model.ssl.cert

                    # Configure auth if present
                    if request_model.auth:
                        try:
                            match request_model.auth:
                                case UserFunctionKwargs():
                                    request_params["auth"] = AuthFunction.call_with_kwargs(request_model.auth.function.root, request_model.auth.kwargs)
                                case UserFunctionName():
                                    request_params["auth"] = AuthFunction.call(request_model.auth.root)
                        except Exception as e:
                            raise RequestError("Failed to configure stage authentication") from e

                    # Add body to params
                    match request_model.body:
                        case None:
                            pass
                        case pytest_httpchain_engine.models.entities.JsonBody(json=data):
                            request_params["json"] = data
                        case pytest_httpchain_engine.models.entities.FormBody(form=data):
                            request_params["data"] = data
                        case pytest_httpchain_engine.models.entities.XmlBody(xml=data):
                            request_params["data"] = data
                        case pytest_httpchain_engine.models.entities.RawBody(raw=data):
                            request_params["data"] = data
                        case pytest_httpchain_engine.models.entities.FilesBody(files=data):
                            request_params["files"] = data

                    # Execute request
                    with ExitStack() as stack:
                        try:
                            if "files" in request_params:
                                request_params["files"] = {field_name: stack.enter_context(open(file_path, "rb")) for field_name, file_path in request_params["files"]}

                            response = cls._session.request(request_model.method.value, request_model.url, **request_params)

                        except FileNotFoundError as e:
                            raise RequestError("File not found for upload") from e
                        except requests.Timeout as e:
                            raise RequestError("HTTP request timed out") from e
                        except requests.ConnectionError as e:
                            raise RequestError("HTTP connection error") from e
                        except requests.RequestException as e:
                            raise RequestError("HTTP request failed") from e
                        except Exception as e:
                            raise RequestError("Unexpected error") from e

                    # Process response and update context
                    context_update: dict[str, Any] = {}
                    response_dict = pytest_httpchain_engine.substitution.walk(stage.response, data_context)
                    response_model = pytest_httpchain_engine.models.entities.Response.model_validate(response_dict)

                    for step in response_model:
                        match step:
                            case pytest_httpchain_engine.models.entities.SaveStep():
                                save_dict = pytest_httpchain_engine.substitution.walk(step.save, data_context)
                                save_model = Save.model_validate(save_dict)

                                # Save data directly
                                result: dict[str, Any] = {}

                                # Save variables from response
                                if len(save_model.vars) > 0:
                                    # Get JSON from response
                                    try:
                                        response_json = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                                    except requests.JSONDecodeError as e:
                                        raise ResponseError("Error getting JSON from response") from e

                                    # Save variables using JMESPath
                                    for var_name, jmespath_expr in save_model.vars.items():
                                        try:
                                            saved_value = jmespath.search(jmespath_expr, response_json)
                                            result[var_name] = saved_value
                                        except jmespath.exceptions.JMESPathError as e:
                                            raise ResponseError(f"Error saving variable {var_name}") from e

                                # Execute save functions
                                for func_item in save_model.functions:
                                    try:
                                        match func_item:
                                            case UserFunctionKwargs():
                                                func_result = VerificationFunction.call_with_kwargs(func_item.function.root, response, func_item.kwargs)
                                            case UserFunctionName():
                                                func_result = VerificationFunction.call(func_item.root, response)
                                        result.update(func_result)
                                    except Exception as e:
                                        raise ResponseError(f"Error calling user function {func_item}") from e

                                data_context.update(result)
                                context_update.update(result)

                            case pytest_httpchain_engine.models.entities.VerifyStep():
                                verify_dict = pytest_httpchain_engine.substitution.walk(step.verify, data_context)
                                verify_model = Verify.model_validate(verify_dict)

                                # Verify response directly
                                # Verify status
                                if verify_model.status:
                                    actual_status = response.status_code
                                    expected_status = verify_model.status.value
                                    if actual_status != expected_status:
                                        raise VerificationError(f"Status code doesn't match: expected {expected_status}, got {actual_status}")

                                # Verify headers
                                for header_name, expected_value in verify_model.headers.items():
                                    actual_value = response.headers.get(header_name)
                                    if actual_value != expected_value:
                                        raise VerificationError(f"Header '{header_name}' doesn't match: expected {expected_value}, got {actual_value}")

                                # Verify variables
                                for var_name, expected_value in verify_model.vars.items():
                                    if var_name not in data_context:
                                        raise VerificationError(f"Var '{var_name}' not found in data context")

                                    actual_value = data_context[var_name]
                                    if actual_value != expected_value:
                                        raise VerificationError(f"Var '{var_name}' verification failed: expected {expected_value}, got {actual_value}")

                                # Execute verification functions
                                for func_item in verify_model.functions:
                                    try:
                                        match func_item:
                                            case UserFunctionKwargs():
                                                result = VerificationFunction.call_with_kwargs(func_item.function.root, response, func_item.kwargs)
                                            case UserFunctionName():
                                                result = VerificationFunction.call(func_item.root, response)

                                        if not result:
                                            raise VerificationError(f"Function '{func_item}' verification failed")

                                    except Exception as e:
                                        raise VerificationError(f"Error calling user function '{func_item}'") from e

                                # Verify body
                                if verify_model.body.schema:
                                    # Get JSON from response
                                    try:
                                        response_json = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                                    except requests.JSONDecodeError as e:
                                        raise ResponseError("Error getting JSON from response") from e

                                    schema = verify_model.body.schema
                                    match schema:
                                        case str() | Path():
                                            schema_path = Path(schema)
                                            try:
                                                with schema_path.open() as f:
                                                    schema_dict = json.load(f)
                                            except (
                                                FileNotFoundError,
                                                OSError,
                                                PermissionError,
                                                UnicodeDecodeError,
                                                json.JSONDecodeError,
                                            ) as e:
                                                raise VerificationError(f"Error reading body schema file '{schema_path}'") from e

                                            try:
                                                check_json_schema(schema_dict)
                                            except jsonschema.SchemaError as e:
                                                raise VerificationError(f"Invalid JSON Schema in file '{schema_path}': {e.message}") from e

                                            schema = schema_dict

                                        case dict():
                                            # Use inline schema (already validated by Pydantic)
                                            pass

                                    try:
                                        jsonschema.validate(instance=response_json, schema=schema)
                                    except jsonschema.ValidationError as e:
                                        raise VerificationError("Body schema validation failed") from e
                                    except jsonschema.SchemaError as e:
                                        raise VerificationError("Invalid body validation schema") from e

                                # Verify substring patterns
                                for substring in verify_model.body.contains:
                                    if substring not in response.text:
                                        raise VerificationError(f"Body doesn't contain '{substring}'")

                                for substring in verify_model.body.not_contains:
                                    if substring in response.text:
                                        raise VerificationError(f"Body contains '{substring}' while it shouldn't")

                                # Verify regex patterns
                                for pattern in verify_model.body.matches:
                                    if not re.search(pattern, response.text):
                                        raise VerificationError(f"Body doesn't match '{pattern}'")

                                for pattern in verify_model.body.not_matches:
                                    if re.search(pattern, response.text):
                                        raise VerificationError(f"Body matches '{pattern}' while it shouldn't")

                    cls._data_context.update(context_update)

                except (
                    pytest_httpchain_engine.substitution.SubstitutionError,
                    ValidationError,
                    Exception,
                ) as e:
                    logger.exception(str(e))
                    cls._aborted = True
                    pytest.fail(reason=str(e), pytrace=False)

        # Add stage methods to the carrier class
        for i, stage_canvas in enumerate(scenario.stages):
            # Create stage method - using default argument to capture stage_canvas
            def stage_method(self, *, _stage=stage_canvas, **fixture_kwargs: Any):
                Carrier.execute_stage(_stage, fixture_kwargs)

            # Set up method signature with fixtures
            all_fixtures = ["self"] + stage_canvas.fixtures + scenario.fixtures
            stage_method.__signature__ = inspect.Signature([inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in all_fixtures])

            # Apply markers
            markers = stage_canvas.marks + [f"order({i})"]
            for mark_str in markers:
                try:
                    evaluator = EvalWithCompoundTypes(names={"pytest": pytest})
                    marker = evaluator.eval(f"pytest.mark.{mark_str}")
                    if marker:
                        stage_method = marker(stage_method)
                except Exception as e:
                    logger.warning(f"Failed to create marker '{mark_str}': {e}")

            setattr(Carrier, f"test_{i}_{stage_canvas.name}", stage_method)

        # Create pytest Class node for the carrier
        dummy_module = types.ModuleType("dummy")
        setattr(dummy_module, self.name, Carrier)
        self._getobj = lambda: dummy_module

        json_class = python.Class.from_parent(
            self,
            path=self.path,
            name=self.name,
            obj=Carrier,
        )

        # Apply scenario-level markers
        for mark_str in scenario.marks:
            try:
                evaluator = EvalWithCompoundTypes(names={"pytest": pytest})
                marker = evaluator.eval(f"pytest.mark.{mark_str}")
                if marker:
                    json_class.add_marker(marker)
            except Exception as e:
                logger.warning(f"Failed to create marker '{mark_str}': {e}")

        yield json_class


def pytest_addoption(parser: argparsing.Parser) -> None:
    """Add command-line options for the plugin."""
    parser.addini(
        name=ConfigOptions.SUFFIX,
        help="File suffix for HTTP test files.",
        type="string",
        default="http",
    )
    parser.addini(
        name=ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH,
        help="Maximum number of parent directory traversals allowed in $ref paths.",
        type="string",
        default="3",
    )


def pytest_configure(config: config.Config) -> None:
    """Validate configuration settings."""
    suffix = str(config.getini(ConfigOptions.SUFFIX))
    if not re.match(r"^[a-zA-Z0-9_-]{1,32}$", suffix):
        raise ValueError("suffix must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars")

    ref_parent_traversal_depth = int(config.getini(ConfigOptions.REF_PARENT_TRAVERSAL_DEPTH))
    if ref_parent_traversal_depth < 0:
        raise ValueError("Maximum number of parent directory traversals must be non-negative")


def pytest_collect_file(file_path: Path, parent: nodes.Collector) -> nodes.Collector | None:
    """Collect JSON test files matching the configured pattern."""
    suffix: str = parent.config.getini(ConfigOptions.SUFFIX)
    pattern = re.compile(rf"^test_(?P<name>.+)\.{re.escape(suffix)}\.json$")
    file_match = pattern.match(file_path.name)
    if file_match:
        return JsonModule.from_parent(parent, path=file_path, name=file_match.group("name"))
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: nodes.Item, call: runner.CallInfo):
    """Add custom sections to test reports."""
    outcome = yield
    report: reports.TestReport = outcome.get_result()
    if call.when == "call":
        report.sections.append(("call_title", "call_value"))
