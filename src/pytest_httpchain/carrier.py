import base64
import inspect
import json
import logging
import re
from collections import ChainMap
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import httpx
import jmespath
import jmespath.exceptions
import jsonschema
import pytest
from pydantic import TypeAdapter, ValidationError
from pyrate_limiter import Duration, Limiter, Rate
from pytest_httpchain_models import (
    Base64Body,
    BinaryBody,
    CombinationsParameter,
    FilesBody,
    FormBody,
    GraphQLBody,
    IndividualParameter,
    JMESPathSave,
    JsonBody,
    ParallelForeachConfig,
    ParallelRepeatConfig,
    Request,
    Save,
    SaveStep,
    Scenario,
    SSLConfig,
    Stage,
    SubstitutionsSave,
    TextBody,
    UserFunctionsSave,
    Verify,
    VerifyStep,
    XmlBody,
    check_json_schema,
)
from pytest_httpchain_templates import TemplatesError, walk
from pytest_httpchain_userfunc import UserFunctionError
from simpleeval import EvalWithCompoundTypes

from .exceptions import RequestError, SaveError, StageExecutionError, VerificationError
from .utils import call_user_function, process_substitutions

logger = logging.getLogger(__name__)


@dataclass
class ParallelIterationResult:
    """Result of a successful parallel iteration."""

    saved_context: dict[str, Any]
    request: httpx.Request
    response: httpx.Response


class Carrier:
    """Test carrier class that integrates HTTP chain test execution.

    This base class is subclassed dynamically to create test classes with scenario-specific test methods.
    It manages the shared state, context, and execution flow for all stages in a test scenario.
    """

    client: ClassVar[httpx.Client | None] = None
    aborted: ClassVar[bool] = False
    last_request: ClassVar[httpx.Request | None] = None
    last_response: ClassVar[httpx.Response | None] = None
    global_context: ClassVar[ChainMap[str, Any]] = ChainMap()
    active_context_managers: ClassVar[list[AbstractContextManager]] = []

    @classmethod
    def execute_stage(cls, stage: Stage, fixture_kwargs: dict[str, Any]) -> None:
        try:
            if cls.aborted and not stage.always_run:
                pytest.skip(reason="Flow aborted")

            # prepare stage context
            stage_fixtures: dict[str, Any] = {}
            for name, value in fixture_kwargs.items():
                if callable(value) and not inspect.isclass(value):
                    stage_fixtures[name] = cls._wrap_factory_fixture(value)
                else:
                    stage_fixtures[name] = value

            # Build base context for iterations (substitutions + fixtures + global)
            local_context = ChainMap(stage_fixtures, cls.global_context)
            stage_substitutions = process_substitutions(stage.substitutions, local_context)
            local_context = local_context.new_child(stage_substitutions)

            logger.info(f"global context on start: {json.dumps(dict(cls.global_context), indent=2, default=str)}")
            logger.info(f"local context on start: {json.dumps(dict(local_context), indent=2, default=str)}")

            # build iterations
            iteration_substitutions: list[dict[str, Any]] = [{}]
            parallel_config = walk(stage.parallel, local_context) if stage.parallel else None
            match parallel_config:
                case None:
                    pass
                case ParallelRepeatConfig(repeat=repeat_count):
                    iteration_substitutions = [{} for _ in range(repeat_count)]
                case ParallelForeachConfig(foreach=foreach_steps):
                    # same algorithm like what pytest does for parametrize marker
                    for step in foreach_steps:
                        match step:
                            case IndividualParameter(individual=individual):
                                param_name = next(iter(individual.keys()))
                                values = individual[param_name]
                                iteration_substitutions = [{**existing, param_name: val} for val in values for existing in iteration_substitutions]
                            case CombinationsParameter(combinations=combinations):
                                combos: list[dict[str, Any] | SimpleNamespace] = [vars(item) if isinstance(item, SimpleNamespace) else item for item in combinations]
                                iteration_substitutions = [{**existing, **combo} for combo in combos for existing in iteration_substitutions]

            # execute iterations
            max_concurrency = parallel_config.max_concurrency if parallel_config else 1
            calls_per_sec = parallel_config.calls_per_sec if parallel_config else None

            total = len(iteration_substitutions)
            results: list[ParallelIterationResult | None] = [None] * total
            first_error: tuple[int, Exception] | None = None
            limiter = Limiter(Rate(calls_per_sec, Duration.SECOND), max_delay=Duration.HOUR) if calls_per_sec else None

            if total == 1:
                try:
                    results[0] = cls._execute_single_iteration(stage, local_context, iteration_substitutions[0], limiter)
                except (StageExecutionError, TemplatesError, ValidationError) as e:
                    first_error = (0, e)
            else:
                workers = min(max_concurrency, total) if total > 0 else 1
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures: dict[Future[ParallelIterationResult], int] = {}
                    for idx, iter_vars in enumerate(iteration_substitutions):
                        future = executor.submit(cls._execute_single_iteration, stage, local_context, iter_vars, limiter)
                        futures[future] = idx

                    for future in as_completed(futures):
                        idx = futures[future]
                        try:
                            results[idx] = future.result()
                        except (StageExecutionError, TemplatesError, ValidationError) as e:
                            first_error = (idx, e)
                            executor.shutdown(wait=False, cancel_futures=True)
                            break

            # Apply results in index order - collect all saves for new layer
            all_saves: dict[str, Any] = {}
            for iter_result in results:
                if iter_result is not None:
                    all_saves.update(iter_result.saved_context)
                    cls.last_request = iter_result.request
                    cls.last_response = iter_result.response

            logger.info(f"updates for global context: {json.dumps(all_saves, indent=2, default=str)}")

            # Add response saves as new layer
            cls.global_context = cls.global_context.new_child(all_saves)

            # Handle error
            if first_error:
                idx, exc = first_error
                # Extract request/response from failed iteration for debugging
                if isinstance(exc, StageExecutionError):
                    if exc.request is not None:
                        cls.last_request = exc.request
                    if exc.response is not None:
                        cls.last_response = exc.response
                raise StageExecutionError(f"Parallel execution failed at iteration {idx}: {exc}")

        except (
            TemplatesError,
            StageExecutionError,
            ValidationError,
        ) as e:
            is_xfail = any("xfail" in mark for mark in stage.marks)
            if not is_xfail:
                logger.error(str(e))
                cls.aborted = True
            pytest.fail(reason=str(e), pytrace=False)

    @staticmethod
    def _build_request_kwargs(request_model: Request) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {
            "method": request_model.method,
            "url": str(request_model.url),
            "headers": request_model.headers,
            "params": request_model.params,
            "timeout": request_model.timeout,
            "follow_redirects": request_model.allow_redirects,
        }

        if request_model.auth:
            try:
                auth_result = call_user_function(request_model.auth)
                request_kwargs["auth"] = auth_result
            except UserFunctionError as e:
                raise RequestError(f"Failed to configure authentication: {str(e)}") from None

        match request_model.body:
            case None:
                pass

            case JsonBody(json=data):
                request_kwargs["json"] = data

            case GraphQLBody(graphql=gql):
                request_kwargs["json"] = {"query": gql.query, "variables": gql.variables}

            case FormBody(form=data):
                request_kwargs["data"] = data

            case XmlBody(xml=data) | TextBody(text=data):
                request_kwargs["content"] = data

            case Base64Body(base64=encoded_data):
                decoded_data = base64.b64decode(encoded_data)
                request_kwargs["content"] = decoded_data

            case BinaryBody(binary=file_path):
                try:
                    with open(file_path, "rb") as f:
                        binary_data = f.read()
                    request_kwargs["content"] = binary_data
                except FileNotFoundError:
                    raise RequestError(f"Binary file not found: {file_path}") from None

            case FilesBody(files=file_paths):
                files_list = []
                for field_name, file_path in file_paths.items():
                    try:
                        with open(file_path, "rb") as f:
                            file_content = f.read()
                        files_list.append((field_name, (Path(file_path).name, file_content)))
                    except FileNotFoundError:
                        raise RequestError(f"File not found for upload: {file_path}") from None
                request_kwargs["files"] = files_list

        return request_kwargs

    @classmethod
    def _execute_http_request(cls, request_kwargs: dict[str, Any]) -> httpx.Response:
        try:
            return cls.client.request(**request_kwargs)
        except httpx.TimeoutException as e:
            raise RequestError(f"HTTP request timed out: {str(e)}") from None
        except httpx.ConnectError as e:
            raise RequestError(f"HTTP connection error: {str(e)}") from None
        except httpx.HTTPError as e:
            raise RequestError(f"HTTP request failed: {str(e)}") from None
        except Exception as e:
            raise RequestError(f"Unexpected error: {str(e)}") from None

    @staticmethod
    def _process_save_step(save_model: Save, response: httpx.Response, context: ChainMap[str, Any]) -> dict[str, Any]:
        step_saved: dict[str, Any] = {}

        match save_model:
            case JMESPathSave():
                try:
                    response_json = response.json()
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    raise SaveError(f"Cannot extract variables, response is not valid JSON: {str(e)}") from None

                for var_name, jmespath_expr in save_model.jmespath.items():
                    try:
                        saved_value = jmespath.search(jmespath_expr, response_json)
                        step_saved[var_name] = saved_value
                    except jmespath.exceptions.JMESPathError as e:
                        raise SaveError(f"Error saving variable {var_name}: {str(e)}") from None

            case SubstitutionsSave():
                try:
                    substitution_result = process_substitutions(save_model.substitutions, context)
                    step_saved.update(substitution_result)
                except TemplatesError as e:
                    raise SaveError(f"Error processing substitutions: {str(e)}") from None

            case UserFunctionsSave():
                for func_item in save_model.user_functions:
                    try:
                        func_result = call_user_function(func_item, response=response)

                        if not isinstance(func_result, dict):
                            raise SaveError(f"Save function must return dict, got {type(func_result).__name__}")

                        result_dict = cast(dict[str, Any], func_result)
                        step_saved.update(result_dict)
                    except SaveError:
                        raise
                    except UserFunctionError as e:
                        raise SaveError(f"Error calling user function '{func_item}': {str(e)}") from None

        return step_saved

    @staticmethod
    def _process_verify_step(verify_model: Verify, response: httpx.Response) -> None:
        if verify_model.status and response.status_code != verify_model.status:
            raise VerificationError(f"Status code doesn't match: expected {verify_model.status}, got {response.status_code}")

        for header_name, expected_value in verify_model.headers.items():
            if response.headers.get(header_name) != expected_value:
                raise VerificationError(f"Header '{header_name}' doesn't match: expected {expected_value}, got {response.headers.get(header_name)}")

        for i, expression in enumerate(verify_model.expressions):
            if not expression:
                raise VerificationError(f"Expression {i} failed: evaluated to {expression}")

        for func_item in verify_model.user_functions:
            try:
                result = call_user_function(func_item, response=response)

                if not isinstance(result, bool):
                    raise VerificationError(f"Verify function must return bool, got {type(result).__name__}")

                if not result:
                    raise VerificationError(f"Function '{func_item}' verification failed")

            except VerificationError:
                raise
            except UserFunctionError as e:
                raise VerificationError(f"Error calling user function '{func_item}': {str(e)}") from None

        if verify_model.body.schema:
            schema = verify_model.body.schema
            if isinstance(schema, str | Path):
                schema_path = Path(schema)
                try:
                    schema = json.loads(schema_path.read_text())
                    check_json_schema(schema)
                except (OSError, json.JSONDecodeError) as e:
                    raise VerificationError(f"Error reading body schema file '{schema_path}': {str(e)}") from None
                except jsonschema.SchemaError as e:
                    raise VerificationError(f"Invalid JSON Schema in file '{schema_path}': {e}") from None

            try:
                response_json = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise VerificationError(f"Cannot validate schema, response is not valid JSON: {str(e)}") from None

            try:
                jsonschema.validate(instance=response_json, schema=schema)
            except jsonschema.ValidationError as e:
                raise VerificationError(f"Body schema validation failed: {str(e)}") from None
            except jsonschema.SchemaError as e:
                raise VerificationError(f"Invalid body validation schema: {str(e)}") from None

        for substring in verify_model.body.contains:
            if substring not in response.text:
                raise VerificationError(f"Body doesn't contain '{substring}'")

        for substring in verify_model.body.not_contains:
            if substring in response.text:
                raise VerificationError(f"Body contains '{substring}' while it shouldn't")

        for pattern in verify_model.body.matches:
            if not re.search(pattern, response.text):
                raise VerificationError(f"Body doesn't match '{pattern}'")

        for pattern in verify_model.body.not_matches:
            if re.search(pattern, response.text):
                raise VerificationError(f"Body matches '{pattern}' while it shouldn't")

    @classmethod
    def _execute_single_iteration(cls, stage: Stage, local_context: ChainMap[str, Any], iter_vars: Mapping[str, Any], limiter: Limiter | None = None) -> ParallelIterationResult:
        """Execute a single iteration of a stage."""
        iter_context = local_context.new_child(dict(iter_vars))

        request_dict = walk(stage.request, iter_context)
        request_model = Request.model_validate(request_dict)
        request_kwargs = cls._build_request_kwargs(request_model)

        if limiter:
            limiter.try_acquire("api")

        response = cls._execute_http_request(request_kwargs)

        try:
            saved_context: dict[str, Any] = {}
            for step in stage.response:
                match step:
                    case SaveStep():
                        save_dict = walk(step.save, iter_context)
                        save_model = TypeAdapter(Save).validate_python(save_dict)
                        step_saved = cls._process_save_step(save_model, response, iter_context)
                        iter_context = iter_context.new_child(step_saved)
                        saved_context.update(step_saved)

                    case VerifyStep():
                        verify_dict = walk(step.verify, iter_context)
                        verify_model = Verify.model_validate(verify_dict)
                        cls._process_verify_step(verify_model, response)
        except StageExecutionError as e:
            e.request = response.request
            e.response = response
            raise
        except (TemplatesError, ValidationError) as e:
            raise StageExecutionError(str(e), request=response.request, response=response) from e

        return ParallelIterationResult(
            saved_context=saved_context,
            request=response.request,
            response=response,
        )

    @classmethod
    def _wrap_factory_fixture(cls, fixture: Callable) -> Callable:
        def wrapped(*args, **kwargs):
            result = fixture(*args, **kwargs)

            if isinstance(result, AbstractContextManager):
                value = result.__enter__()
                cls.active_context_managers.append(result)
                return value

            return result

        return wrapped

    @classmethod
    def teardown_class(cls) -> None:
        while cls.active_context_managers:
            ctx = cls.active_context_managers.pop()
            try:
                ctx.__exit__(None, None, None)
            except Exception as e:
                logger.error(f"Error while cleaning up context manager fixture: {str(e)}")

        if cls.client is not None:
            cls.client.close()
            cls.client = None


def create_test_class(scenario: Scenario, class_name: str) -> type[Carrier]:
    """Create a dynamic test class from a scenario definition."""
    scenario_context = process_substitutions(scenario.substitutions)

    resolved_ssl: SSLConfig = walk(scenario.ssl, scenario_context)
    client_kwargs: dict[str, Any] = {
        "verify": resolved_ssl.verify,
        "http2": True,
    }
    if scenario.ssl.cert is not None:
        client_kwargs["cert"] = resolved_ssl.cert
    if scenario.auth:
        resolved_auth = walk(scenario.auth, scenario_context)
        auth_result = call_user_function(resolved_auth)
        client_kwargs["auth"] = auth_result

    client = httpx.Client(**client_kwargs)

    CustomCarrier = type(
        class_name,
        (Carrier,),
        {
            "__doc__": scenario.description,
            "client": client,
            "aborted": False,
            "last_request": None,
            "last_response": None,
            "global_context": ChainMap(scenario_context),
            "active_context_managers": [],
        },
    )

    total_stages = len(scenario.stages)
    padding_width = len(str(total_stages - 1)) if total_stages > 0 else 1

    for i, stage in enumerate(scenario.stages):

        def make_stage_method(stage_template: Stage) -> Callable:
            def call_execute_stage(self, **kwargs):
                type(self).execute_stage(stage_template, kwargs)

            return call_execute_stage

        stage_method = make_stage_method(stage)

        if stage.description:
            stage_method.__doc__ = stage.description

        all_param_names = []

        if stage.parametrize:
            for step in stage.parametrize:
                match step:
                    case IndividualParameter(individual=individual) if individual:
                        param_name = next(iter(individual.keys()))
                        param_values = individual[param_name]
                        resolved_values = walk(param_values, scenario_context)

                        param_ids = step.ids if step.ids else None

                        all_param_names.append(param_name)
                        parametrize_marker = pytest.mark.parametrize(param_name, resolved_values, ids=param_ids)
                        stage_method = parametrize_marker(stage_method)

                    case CombinationsParameter(combinations=combinations) if combinations:
                        resolved_combinations = walk(combinations, scenario_context)
                        resolved_combinations = [vars(item) if isinstance(item, SimpleNamespace) else item for item in resolved_combinations]

                        first_item = resolved_combinations[0]
                        param_names = list(first_item.keys())
                        param_values = [tuple(combo[name] for name in param_names) for combo in resolved_combinations]
                        param_ids = step.ids if step.ids else None

                        all_param_names.extend(param_names)
                        parametrize_marker = pytest.mark.parametrize(",".join(param_names), param_values, ids=param_ids)
                        stage_method = parametrize_marker(stage_method)

        all_fixtures = ["self"] + all_param_names + stage.fixtures
        stage_method.__signature__ = inspect.Signature([inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in all_fixtures])  # type: ignore[assignment]

        all_marks = [f"order({i})"] + stage.marks
        evaluator = EvalWithCompoundTypes(names={"pytest": pytest})
        for mark_str in all_marks:
            try:
                marker = evaluator.eval(f"pytest.mark.{mark_str}")
                if marker:
                    stage_method = marker(stage_method)
            except Exception as e:
                logger.warning(f"Failed to create marker '{mark_str}': {e}")

        method_name = f"test {str(i).zfill(padding_width)} - {stage.name}"
        setattr(CustomCarrier, method_name, stage_method)

    return cast(type[Carrier], CustomCarrier)
