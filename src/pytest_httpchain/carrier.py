"""Test carrier class for HTTP chain test execution.

The Carrier class manages the test lifecycle and infrastructure:
- HTTP session initialization and cleanup
- Global context state management
- Test flow control (abort handling)
- Integration with pytest (skip, fail)
- Stage execution orchestration
- Dynamic test class generation from scenarios
"""

import inspect
import logging
from types import SimpleNamespace
from typing import Any, ClassVar, Self

import pytest
import pytest_httpchain_templates.substitution
import requests
from pydantic import ValidationError
from pytest_httpchain_models.entities import (
    CombinationsStep,
    IndividualStep,
    Request,
    Save,
    SaveStep,
    Scenario,
    Stage,
    UserFunctionKwargs,
    UserFunctionName,
    Verify,
    VerifyStep,
)
from pytest_httpchain_templates.exceptions import TemplatesError
from pytest_httpchain_userfunc import wrap_function
from simpleeval import EvalWithCompoundTypes

from .context_manager import ContextManager
from .exceptions import StageExecutionError
from .request import execute_request, prepare_request
from .response import process_save_step, process_verify_step
from .utils import call_user_function

logger = logging.getLogger(__name__)


class Carrier:
    """Test carrier class that integrates HTTP chain test execution.

    This base class is subclassed dynamically by carrier_factory to create
    test classes with scenario-specific test methods. It manages the shared
    state and execution flow for all stages in a test scenario.

    Attributes:
        _scenario: The test scenario configuration
        _session: Shared HTTP session for all stages
        _aborted: Flag indicating if test flow should be aborted
    """

    _scenario: ClassVar[Scenario]
    _session: ClassVar[requests.Session | None] = None
    _context_manager: ClassVar[ContextManager | None] = None
    _aborted: ClassVar[bool] = False
    _last_request: ClassVar[requests.PreparedRequest | None] = None
    _last_response: ClassVar[requests.Response | None] = None

    @classmethod
    def execute_stage(cls, stage_template: Stage, fixture_kwargs: dict[str, Any]) -> None:
        """Execute a test stage with abort handling and error management.

        This method is called for each stage in the scenario. It handles:
        - Checking abort status and skipping if needed
        - Context preparation and template substitution
        - HTTP request execution and response processing
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

            if cls._session is None:
                raise RuntimeError("Session not initialized")

            if cls._context_manager is None:
                raise RuntimeError("Context manager not initialized")

            # Prepare the context for this stage
            local_context = cls._context_manager.prepare_stage_context(
                scenario=cls._scenario,
                stage=stage_template,
                fixture_kwargs=fixture_kwargs,
            )

            # Debug: check seed context
            if cls._context_manager.seed_context:
                # Filter to show only functions (callables) in the seed context
                functions = [k for k, v in cls._context_manager.seed_context.items() if callable(v)]
                if functions:
                    logger.debug(f"Functions available in context: {functions}")

            request_dict = pytest_httpchain_templates.substitution.walk(stage_template.request, local_context)
            request_model = Request.model_validate(request_dict)

            prepared = prepare_request(cls._session, request_model)
            cls._last_request = prepared.request

            response = execute_request(cls._session, prepared)
            cls._last_response = response

            global_context_updates: dict[str, Any] = {}

            for step in stage_template.response:
                match step:
                    case SaveStep():
                        save_dict = pytest_httpchain_templates.substitution.walk(step.save, local_context)
                        save_model = Save.model_validate(save_dict)
                        saved_vars = process_save_step(save_model, response)
                        local_context = local_context.new_child(saved_vars)
                        global_context_updates.update(saved_vars)

                    case VerifyStep():
                        verify_dict = pytest_httpchain_templates.substitution.walk(step.verify, local_context)
                        verify_model = Verify.model_validate(verify_dict)
                        process_verify_step(verify_model, local_context, response)

            cls._context_manager.update_global_context(global_context_updates)

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

        # Process substitution steps to create seed context for ContextManager
        # This context will be passed to ContextManager at runtime initialization
        seed_context = {}

        for step in scenario.substitutions:
            # Load and wrap functions for this step
            if step.functions:
                for alias, func_def in step.functions.items():
                    match func_def:
                        case UserFunctionName():
                            # Model with just function name
                            seed_context[alias] = wrap_function(func_def.root)
                        case UserFunctionKwargs():
                            # Model with name and kwargs
                            seed_context[alias] = wrap_function(func_def.name.root, func_def.kwargs)
                        case _:
                            raise StageExecutionError(f"Invalid function definition for '{alias}': expected UserFunctionName or UserFunctionKwargs")

                logger.debug(f"Loaded {len(step.functions)} functions in substitution step")

            # Process vars for this step with accumulated context
            if step.vars:
                # Process each var sequentially within the step
                # This allows later vars to reference earlier ones within the same step
                for var_name, var_value in step.vars.items():
                    resolved_value = pytest_httpchain_templates.substitution.walk(
                        var_value,
                        seed_context,
                    )
                    logger.debug(f"Processed scenario var at collection: {var_name} = {resolved_value}")
                    # Add to seed context immediately so next var can use it
                    seed_context[var_name] = resolved_value

        # Use seed context for parameter substitution
        param_context = seed_context

        # Initialize session and context manager
        session = requests.Session()
        context_manager = ContextManager(seed_context=seed_context)

        if seed_context:
            logger.info(f"Initialized context with {len(seed_context)} seed items")

        # Configure SSL settings
        session.verify = scenario.ssl.verify
        if scenario.ssl.cert is not None:
            session.cert = scenario.ssl.cert

        # Configure authentication
        if scenario.auth:
            # Use seed context for auth substitution
            resolved_auth = pytest_httpchain_templates.substitution.walk(scenario.auth, seed_context)
            # Call the auth function using the helper
            auth_result = call_user_function(resolved_auth)
            session.auth = auth_result

        # Create custom Carrier class with scenario, initialized session and context
        CustomCarrier = type(
            class_name,
            (cls,),  # Use cls instead of Carrier for better inheritance support
            {
                "_scenario": scenario,
                "_session": session,
                "_context_manager": context_manager,
                "_aborted": False,
                "_last_request": None,
                "_last_response": None,
            },
        )

        for i, stage in enumerate(scenario.stages):
            # Create test method - capture stage in closure
            def make_stage_method(stage_template):
                def stage_method_impl(self, **kwargs):
                    type(self).execute_stage(stage_template, kwargs)

                return stage_method_impl

            stage_method = make_stage_method(stage)

            if stage.description:
                stage_method.__doc__ = stage.description

            # Collect all parameter names for signature
            all_param_names = []

            # Apply parametrize marks if present (creates cartesian product)
            if stage.parameters:
                for step in stage.parameters:
                    if isinstance(step, IndividualStep):
                        # Single parameter step
                        if step.individual:  # Skip if empty
                            param_name = next(iter(step.individual.keys()))
                            param_values = step.individual[param_name]
                            resolved_values = pytest_httpchain_templates.substitution.walk(param_values, param_context)

                            param_ids = step.ids if step.ids else None

                            all_param_names.append(param_name)
                            parametrize_marker = pytest.mark.parametrize(param_name, resolved_values, ids=param_ids)
                            stage_method = parametrize_marker(stage_method)

                    elif isinstance(step, CombinationsStep):
                        # Multiple parameters in combination
                        if step.combinations:
                            resolved_combinations = pytest_httpchain_templates.substitution.walk(step.combinations, param_context)
                            resolved_combinations = [vars(item) if isinstance(item, SimpleNamespace) else item for item in resolved_combinations]

                            first_item = resolved_combinations[0]
                            param_names = list(first_item.keys())
                            param_values = [tuple(combo[name] for name in param_names) for combo in resolved_combinations]
                            param_ids = step.ids if step.ids else None

                            all_param_names.extend(param_names)
                            parametrize_marker = pytest.mark.parametrize(",".join(param_names), param_values, ids=param_ids)
                            stage_method = parametrize_marker(stage_method)

            # Set fixtures - parameters will be added to kwargs by pytest
            all_fixtures = ["self"] + all_param_names + stage.fixtures + scenario.fixtures
            stage_method.__signature__ = inspect.Signature([inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in all_fixtures])

            # Apply other markers
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
