import inspect
import logging
from collections import ChainMap
from types import SimpleNamespace
from typing import Any, ClassVar, Self

import httpx
import pytest
import pytest_httpchain_templates.substitution
from pydantic import ValidationError
from pytest_httpchain_models.entities import (
    CombinationsParameter,
    IndividualParameter,
    ParallelConfig,
    Request,
    Save,
    SaveStep,
    Scenario,
    SSLConfig,
    Stage,
    Verify,
    VerifyStep,
)
from pytest_httpchain_templates.exceptions import TemplatesError
from simpleeval import EvalWithCompoundTypes

from .context_manager import ContextManager
from .exceptions import StageExecutionError
from .parallel import ParallelIterationError, ParallelIterationResult, execute_parallel_requests
from .request import execute_request, prepare_request
from .response import process_save_step, process_verify_step
from .utils import call_user_function, process_substitutions

logger = logging.getLogger(__name__)


class Carrier:
    """Test carrier class that integrates HTTP chain test execution.

    This base class is subclassed dynamically by carrier_factory to create
    test classes with scenario-specific test methods. It manages the shared
    state and execution flow for all stages in a test scenario.
    """

    _scenario: ClassVar[Scenario]
    _client: ClassVar[httpx.Client | None] = None
    _context_manager: ClassVar[ContextManager | None] = None
    _aborted: ClassVar[bool] = False
    _last_request: ClassVar[httpx.Request | None] = None
    _last_response: ClassVar[httpx.Response | None] = None

    @classmethod
    def execute_stage(cls, stage_template: Stage, fixture_kwargs: dict[str, Any]) -> None:
        """Execute a test stage with abort handling and error management.

        This method is called for each stage in the scenario. It handles:
        - Checking abort status and skipping if needed
        - Context preparation and template substitution
        - HTTP request execution and response processing (single or parallel)
        - Updating global context with saved variables
        - Setting abort flag on errors (unless stage is marked with xfail)

        Args:
            stage_template: The stage configuration containing request/response definitions
            fixture_kwargs: Dictionary of pytest fixture values injected for this stage

        Raises:
            pytest.skip: If flow is aborted and stage doesn't have always_run=True
            pytest.fail: If stage execution fails with an error

        Note:
            Sets cls._aborted to True on failure, causing subsequent stages
            to be skipped unless they have always_run=True. However, if the stage
            is marked with xfail, the abort flag is not set, allowing execution
            to continue normally.
        """
        try:
            if cls._aborted and not stage_template.always_run:
                pytest.skip(reason="Flow aborted")

            if cls._client is None:
                raise RuntimeError("Client not initialized")

            if cls._context_manager is None:
                raise RuntimeError("Context manager not initialized")

            local_context = cls._context_manager.prepare_stage_context(
                scenario=cls._scenario,
                stage=stage_template,
                fixture_kwargs=fixture_kwargs,
            )

            if stage_template.parallel is not None:
                cls._execute_parallel_stage(stage_template, local_context)
            else:
                cls._execute_single_stage(stage_template, local_context)

        except (
            TemplatesError,
            StageExecutionError,
            ValidationError,
        ) as e:
            is_xfail = any("xfail" in mark for mark in stage_template.marks)
            if not is_xfail:
                logger.error(str(e))
                cls._aborted = True
            pytest.fail(reason=str(e), pytrace=False)

    @classmethod
    def _execute_single_stage(cls, stage_template: Stage, local_context: ChainMap[str, Any]) -> None:
        """Execute a single HTTP request stage."""
        request_dict = pytest_httpchain_templates.substitution.walk(stage_template.request, local_context)
        request_model = Request.model_validate(request_dict)

        prepared = prepare_request(cls._client, request_model)
        response = execute_request(cls._client, prepared)
        cls._last_request = prepared.last_request
        cls._last_response = response

        global_context_updates: dict[str, Any] = {}

        for step in stage_template.response:
            match step:
                case SaveStep():
                    save_dict = pytest_httpchain_templates.substitution.walk(step.save, local_context)
                    save_model = Save.model_validate(save_dict)
                    saved_vars = process_save_step(save_model, local_context, response)
                    local_context = local_context.new_child(saved_vars)
                    global_context_updates.update(saved_vars)

                case VerifyStep():
                    verify_dict = pytest_httpchain_templates.substitution.walk(step.verify, local_context)
                    verify_model = Verify.model_validate(verify_dict)
                    process_verify_step(verify_model, local_context, response)

        cls._context_manager.update_global_context(global_context_updates)

    @classmethod
    def _execute_parallel_stage(cls, stage_template: Stage, base_context: ChainMap[str, Any]) -> None:
        """Execute stage with parallel HTTP requests."""
        parallel_config = stage_template.parallel
        iterations = cls._build_iterations(parallel_config, base_context)

        if not iterations:
            return

        def execute_single(idx: int, iter_vars: dict[str, Any]) -> ParallelIterationResult:
            iter_context = base_context.new_child(iter_vars)
            return cls._execute_request_internal(stage_template, iter_context, idx)

        result = execute_parallel_requests(
            iterations=iterations,
            execute_fn=execute_single,
            max_concurrency=parallel_config.max_concurrency,
            fail_fast=parallel_config.fail_fast,
        )

        # Merge saved variables in index order (last write wins)
        merged_saves: dict[str, Any] = {}
        last_request = None
        last_response = None

        for iter_result in result.results:
            if isinstance(iter_result, ParallelIterationResult):
                merged_saves.update(iter_result.saved_vars)
                last_request = iter_result.request
                last_response = iter_result.response

        cls._context_manager.update_global_context(merged_saves)
        cls._last_request = last_request
        cls._last_response = last_response

        # Handle errors
        if result.first_error:
            error = result.first_error
            raise StageExecutionError(f"Parallel execution failed at iteration {error.index}: {error.exception}")

        if result.failed_count > 0:
            errors = [r for r in result.results if isinstance(r, ParallelIterationError)]
            error_summary = "; ".join(f"[{e.index}]: {e.exception}" for e in errors)
            raise StageExecutionError(f"Parallel execution had {result.failed_count} failures: {error_summary}")

    @classmethod
    def _execute_request_internal(
        cls,
        stage_template: Stage,
        local_context: ChainMap[str, Any],
        iteration_index: int,
    ) -> ParallelIterationResult:
        """Execute a single request and return results without mutating global state.

        This method is thread-safe as it:
        - Uses thread-safe httpx.Client for HTTP
        - Creates isolated ChainMap for context
        - Does not mutate any shared state
        """
        request_dict = pytest_httpchain_templates.substitution.walk(stage_template.request, local_context)
        request_model = Request.model_validate(request_dict)

        prepared = prepare_request(cls._client, request_model)
        response = execute_request(cls._client, prepared)

        saved_vars: dict[str, Any] = {}

        for step in stage_template.response:
            match step:
                case SaveStep():
                    save_dict = pytest_httpchain_templates.substitution.walk(step.save, local_context)
                    save_model = Save.model_validate(save_dict)
                    step_saved = process_save_step(save_model, local_context, response)
                    local_context = local_context.new_child(step_saved)
                    saved_vars.update(step_saved)

                case VerifyStep():
                    verify_dict = pytest_httpchain_templates.substitution.walk(step.verify, local_context)
                    verify_model = Verify.model_validate(verify_dict)
                    process_verify_step(verify_model, local_context, response)

        return ParallelIterationResult(
            index=iteration_index,
            saved_vars=saved_vars,
            request=prepared.last_request,
            response=response,
        )

    @classmethod
    def _build_iterations(cls, parallel: ParallelConfig, context: ChainMap[str, Any]) -> list[dict[str, Any]]:
        """Build iteration context dicts for parallel execution."""
        if parallel.repeat is not None:
            repeat_val = parallel.repeat
            if isinstance(repeat_val, str):
                repeat_val = pytest_httpchain_templates.substitution.walk(repeat_val, context)
            count = int(repeat_val)
            return [{} for _ in range(count)]

        elif parallel.foreach is not None:
            iterations: list[dict[str, Any]] = [{}]
            for step in parallel.foreach:
                if isinstance(step, IndividualParameter):
                    param_name = next(iter(step.individual.keys()))
                    values = pytest_httpchain_templates.substitution.walk(step.individual[param_name], context)
                    iterations = [{**existing, param_name: val} for val in values for existing in iterations]
                elif isinstance(step, CombinationsParameter):
                    combos = pytest_httpchain_templates.substitution.walk(step.combinations, context)
                    combos = [vars(item) if isinstance(item, SimpleNamespace) else item for item in combos]
                    iterations = [{**existing, **combo} for combo in combos for existing in iterations]
            return iterations

        return []

    @classmethod
    def teardown_class(cls) -> None:
        if cls._context_manager is not None:
            cls._context_manager.cleanup()
            cls._context_manager = None
        if cls._client is not None:
            cls._client.close()
            cls._client = None

    @classmethod
    def create_test_class(cls, scenario: Scenario, class_name: str) -> type[Self]:
        """Create a dynamic test class for the given scenario.

        This factory method generates a pytest test class with:
        - One test method per stage in the scenario
        - Automatic fixture injection based on stage requirements
        - Marker application (order, skip, xfail, etc.)
        - Shared session and context management

        The generated class structure:
        - Inherits from Carrier base class
        - Has test_0_<stage_name>, test_1_<stage_name>, etc. methods
        - Each method requests fixtures defined in stage and scenario
        - Methods are ordered using pytest-order plugin
        - Session and context manager are initialized at class creation time

        Args:
            scenario: Validated scenario configuration containing stages
            class_name: Name for the generated test class

        Returns:
            A Carrier subclass with test methods for each stage

        Example:
            >>> scenario = Scenario.model_validate(test_data)
            >>> TestClass = Carrier.create_test_class(scenario, "TestAPI")
            >>> # TestClass will have methods: test_00_stage1, test_01_stage2, etc. (with zero-padding)
        """
        total_stages = len(scenario.stages)
        padding_width = len(str(total_stages - 1)) if total_stages > 0 else 1

        # Process scenario-level substitutions to build initial context
        scenario_context = process_substitutions(scenario.substitutions, {})

        context_manager = ContextManager(seed_context=scenario_context)

        # Build httpx.Client constructor kwargs
        resolved_ssl: SSLConfig = pytest_httpchain_templates.substitution.walk(scenario.ssl, scenario_context)
        client_kwargs: dict[str, Any] = {
            "verify": resolved_ssl.verify,
        }
        if scenario.ssl.cert is not None:
            client_kwargs["cert"] = resolved_ssl.cert

        if scenario.auth:
            resolved_auth = pytest_httpchain_templates.substitution.walk(scenario.auth, scenario_context)
            auth_result = call_user_function(resolved_auth)
            client_kwargs["auth"] = auth_result

        client = httpx.Client(**client_kwargs)

        class_dict = {
            "_scenario": scenario,
            "_client": client,
            "_context_manager": context_manager,
            "_aborted": False,
            "_last_request": None,
            "_last_response": None,
        }

        if scenario.description:
            class_dict["__doc__"] = scenario.description

        CustomCarrier = type(
            class_name,
            (cls,),
            class_dict,
        )

        for i, stage in enumerate(scenario.stages):

            def make_stage_method(stage_template):
                def stage_method_impl(self, **kwargs):
                    type(self).execute_stage(stage_template, kwargs)

                return stage_method_impl

            stage_method = make_stage_method(stage)

            if stage.description:
                stage_method.__doc__ = stage.description

            all_param_names = []

            if stage.parametrize:
                for step in stage.parametrize:
                    if isinstance(step, IndividualParameter):
                        if step.individual:
                            param_name = next(iter(step.individual.keys()))
                            param_values = step.individual[param_name]
                            resolved_values = pytest_httpchain_templates.substitution.walk(param_values, scenario_context)

                            param_ids = step.ids if step.ids else None

                            all_param_names.append(param_name)
                            parametrize_marker = pytest.mark.parametrize(param_name, resolved_values, ids=param_ids)
                            stage_method = parametrize_marker(stage_method)

                    elif isinstance(step, CombinationsParameter):
                        if step.combinations:
                            resolved_combinations = pytest_httpchain_templates.substitution.walk(step.combinations, scenario_context)
                            resolved_combinations = [vars(item) if isinstance(item, SimpleNamespace) else item for item in resolved_combinations]

                            first_item = resolved_combinations[0]
                            param_names = list(first_item.keys())
                            param_values = [tuple(combo[name] for name in param_names) for combo in resolved_combinations]
                            param_ids = step.ids if step.ids else None

                            all_param_names.extend(param_names)
                            parametrize_marker = pytest.mark.parametrize(",".join(param_names), param_values, ids=param_ids)
                            stage_method = parametrize_marker(stage_method)

            all_fixtures = ["self"] + all_param_names + stage.fixtures
            stage_method.__signature__ = inspect.Signature([inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in all_fixtures])

            all_marks = [f"order({i})"] + stage.marks
            evaluator = EvalWithCompoundTypes(names={"pytest": pytest})
            for mark_str in all_marks:
                try:
                    marker = evaluator.eval(f"pytest.mark.{mark_str}")
                    if marker:
                        stage_method = marker(stage_method)
                except Exception as e:
                    logger.warning(f"Failed to create marker '{mark_str}': {e}")

            method_name = f"test_{str(i).zfill(padding_width)}_{stage.name}"
            setattr(CustomCarrier, method_name, stage_method)

        return CustomCarrier
