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
from typing import Any, ClassVar, Self

import pytest
import pytest_httpchain_templates.substitution
import requests
from pydantic import ValidationError
from pytest_httpchain_models.entities import (
    Request,
    Save,
    SaveStep,
    Scenario,
    Stage,
    Verify,
    VerifyStep,
)
from pytest_httpchain_templates.exceptions import TemplatesError
from pytest_httpchain_userfunc.auth import call_auth_function
from simpleeval import EvalWithCompoundTypes

from .context_manager import ContextManager
from .exceptions import StageExecutionError
from .helpers import call_user_function
from .request import execute_request, prepare_request
from .response import process_save_step, process_verify_step

logger = logging.getLogger(__name__)


class Carrier:
    """Test carrier class that integrates HTTP chain test execution.

    This base class is subclassed dynamically by carrier_factory to create
    test classes with scenario-specific test methods. It manages the shared
    state and execution flow for all stages in a test scenario.

    Attributes:
        _scenario: The test scenario configuration
        _session: Shared HTTP session for all stages
        _data_context: Global context shared across all stages
        _aborted: Flag indicating if test flow should be aborted
    """

    _scenario: ClassVar[Scenario]
    _session: ClassVar[requests.Session | None] = None
    _context_manager: ClassVar[ContextManager | None] = None
    _aborted: ClassVar[bool] = False
    _last_request: ClassVar[requests.PreparedRequest | None] = None
    _last_response: ClassVar[requests.Response | None] = None

    @classmethod
    def setup_class(cls) -> None:
        """Initialize the HTTP session and data context.

        Called once before any test methods in the class are executed.
        Sets up:
        - Empty data context for variable storage
        - HTTP session with SSL and authentication configuration
        - Factory fixture manager for handling callable fixtures

        Note:
            Authentication can be configured at scenario level and will
            be applied to all requests unless overridden at stage level.
        """
        cls._context_manager = ContextManager()
        cls._session = requests.Session()

        # Configure SSL settings
        cls._session.verify = cls._scenario.ssl.verify
        if cls._scenario.ssl.cert is not None:
            cls._session.cert = cls._scenario.ssl.cert

        # Configure authentication
        if cls._scenario.auth:
            resolved_auth = pytest_httpchain_templates.substitution.walk(cls._scenario.auth, cls._data_context)
            auth_instance = call_user_function(resolved_auth, call_auth_function)
            cls._session.auth = auth_instance

    @classmethod
    def teardown_class(cls) -> None:
        """Clean up the HTTP session and reset state.

        Called once after all test methods in the class have been executed.
        Ensures proper cleanup of resources and state reset for next test class.
        """
        if cls._context_manager:
            cls._context_manager.cleanup()
            cls._context_manager = None
        if cls._session:
            cls._session.close()
            cls._session = None
        cls._aborted = False
        cls._last_request = None
        cls._last_response = None

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
                raise RuntimeError("Session not initialized - setup_class was not called")

            if cls._context_manager is None:
                raise RuntimeError("Context manager not initialized - setup_class was not called")

            # Prepare the context for this stage
            local_context = cls._context_manager.prepare_stage_context(
                scenario=cls._scenario,
                stage=stage_template,
                fixture_kwargs=fixture_kwargs,
            )

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
        # Create custom Carrier class with scenario bound
        CustomCarrier = type(
            class_name,
            (cls,),  # Use cls instead of Carrier for better inheritance support
            {
                "_scenario": scenario,
                "_session": None,
                "_data_context": {},
                "_aborted": False,
                "_last_request": None,
                "_last_response": None,
                "_fixture_manager": None,
            },
        )

        total_stages = len(scenario.stages)
        padding_width = len(str(total_stages - 1)) if total_stages > 0 else 1

        for i, stage in enumerate(scenario.stages):
            # Create stage method - using default argument to capture stage
            def stage_method(self, *, _stage: Stage = stage, **fixture_kwargs: dict[str, Any]) -> None:
                type(self).execute_stage(_stage, fixture_kwargs)

            if stage.description:
                stage_method.__doc__ = stage.description

            all_fixtures: list[str] = ["self"] + stage.fixtures + scenario.fixtures
            stage_method.__signature__ = inspect.Signature([inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in all_fixtures])  # type: ignore

            all_marks: list[str] = [f"order({i})"] + stage.marks
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
