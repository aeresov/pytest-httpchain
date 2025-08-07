"""Test carrier class for HTTP chain test execution.

The Carrier class manages the test lifecycle and infrastructure:
- HTTP session initialization and cleanup
- Global context state management
- Test flow control (abort handling)
- Integration with pytest (skip, fail)

The actual HTTP execution and data processing is delegated to stage_executor.
"""

import logging
from typing import Any

import pytest
import pytest_httpchain_templates.substitution
import requests
from pydantic import ValidationError
from pytest_httpchain_models.entities import Scenario, Stage, UserFunctionKwargs
from pytest_httpchain_templates.exceptions import TemplatesError
from pytest_httpchain_userfunc.auth import call_auth_function

from . import stage_executor

logger = logging.getLogger(__name__)


class Carrier:
    """Test carrier class that integrates HTTP chain test execution."""

    _scenario: Scenario
    _session: requests.Session | None = None
    _data_context: dict[str, Any] = {}  # Global context shared across all stages
    _aborted: bool = False

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
            resolved_auth = pytest_httpchain_templates.substitution.walk(cls._scenario.auth, cls._data_context)

            if isinstance(resolved_auth, UserFunctionKwargs):
                auth_instance = call_auth_function(resolved_auth.function.root, **resolved_auth.kwargs)
            else:  # UserFunctionName
                auth_instance = call_auth_function(resolved_auth.root)

            cls._session.auth = auth_instance

    @classmethod
    def teardown_class(cls) -> None:
        """Clean up the HTTP session and reset state."""
        if cls._session:
            cls._session.close()
            cls._session = None
        cls._data_context.clear()
        cls._aborted = False

    @classmethod
    def execute_stage(cls, stage_template: Stage, fixture_kwargs: dict[str, Any]) -> None:
        """Execute a test stage with abort handling and error management."""
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
            stage_executor.StageExecutionError,  # Catches all derived exceptions
            ValidationError,
        ) as e:
            logger.exception(str(e))
            cls._aborted = True
            pytest.fail(reason=str(e), pytrace=False)
