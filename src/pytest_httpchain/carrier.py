"""Test carrier class for HTTP chain test execution.

The Carrier class manages the test lifecycle and infrastructure:
- HTTP session initialization and cleanup
- Global context state management
- Test flow control (abort handling)
- Integration with pytest (skip, fail)

The actual HTTP execution and data processing is delegated to stage_executor.
"""

import logging
from typing import Any, ClassVar

import pytest
import pytest_httpchain_templates.substitution
import requests
from pydantic import ValidationError
from pytest_httpchain_models.entities import Scenario, Stage
from pytest_httpchain_templates.exceptions import TemplatesError
from pytest_httpchain_userfunc.auth import call_auth_function

from . import stage_executor
from .exceptions import StageExecutionError
from .helpers import call_user_function

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
    _data_context: ClassVar[dict[str, Any]] = {}
    _aborted: ClassVar[bool] = False

    @classmethod
    def setup_class(cls) -> None:
        """Initialize the HTTP session and data context.

        Called once before any test methods in the class are executed.
        Sets up:
        - Empty data context for variable storage
        - HTTP session with SSL and authentication configuration

        Note:
            Authentication can be configured at scenario level and will
            be applied to all requests unless overridden at stage level.
        """
        cls._data_context = {}
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
        if cls._session:
            cls._session.close()
            cls._session = None
        cls._data_context.clear()
        cls._aborted = False

    @classmethod
    def execute_stage(cls, stage_template: Stage, fixture_kwargs: dict[str, Any]) -> None:
        """Execute a test stage with abort handling and error management.

        This method is called for each stage in the scenario. It handles:
        - Checking abort status and skipping if needed
        - Executing the stage via stage_executor
        - Updating global context with saved variables
        - Setting abort flag on errors

        Args:
            stage_template: The stage configuration containing request/response definitions
            fixture_kwargs: Dictionary of pytest fixture values injected for this stage

        Raises:
            pytest.skip: If flow is aborted and stage doesn't have always_run=True
            pytest.fail: If stage execution fails with an error

        Note:
            Sets cls._aborted to True on failure, causing subsequent stages
            to be skipped unless they have always_run=True.
        """
        try:
            # Check abort status
            if cls._aborted and not stage_template.always_run:
                pytest.skip(reason="Flow aborted")

            # Verify session is initialized
            if cls._session is None:
                raise RuntimeError("Session not initialized - setup_class was not called")

            # Execute stage and get variables to save globally
            context_updates = stage_executor.execute_stage(
                stage_template=stage_template,
                scenario=cls._scenario,
                session=cls._session,
                global_context=cls._data_context,  # Pass current global state
                fixture_kwargs=fixture_kwargs,
            )

            # Merge returned updates into global context for next stages
            cls._data_context.update(context_updates)

        except (
            TemplatesError,
            StageExecutionError,
            ValidationError,
        ) as e:
            logger.exception(str(e))
            cls._aborted = True
            pytest.fail(reason=str(e), pytrace=False)
