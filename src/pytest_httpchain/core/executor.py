import logging
from copy import deepcopy
from typing import Any

import pytest
import pytest_httpchain_engine.models.entities
import pytest_httpchain_engine.substitution
import requests
from pydantic import ValidationError
from pytest_httpchain_engine.models.entities import Request, Save, Scenario, Stage, Verify

from pytest_httpchain.core.session import HTTPSessionManager
from pytest_httpchain.handlers import RequestHandler, ResponseHandler, VerificationHandler

logger = logging.getLogger(__name__)


class StageExecutor:
    """Handles the execution of test stages."""

    def __init__(self, session_manager: HTTPSessionManager, scenario: Scenario):
        self.session_manager = session_manager
        self.scenario = scenario
        self._aborted = False

    def is_aborted(self) -> bool:
        """Check if the flow has been aborted."""
        return self._aborted

    def abort(self) -> None:
        """Mark the flow as aborted."""
        self._aborted = True

    def execute_stage(self, stage_template: Stage, fixture_kwargs: dict[str, Any]) -> None:
        """Execute a single test stage."""
        try:
            # Prepare data context
            data_context = self._prepare_data_context(stage_template, fixture_kwargs)

            # Prepare and validate Stage
            stage = pytest_httpchain_engine.substitution.walk(stage_template, data_context)

            # Skip if the flow is aborted
            if self._aborted and not stage.always_run:
                pytest.skip(reason="Flow aborted")

            # Make HTTP call
            response = self._make_http_call(stage, data_context)

            context_update = self._process_response(stage, response, data_context)
            self.session_manager.update_data_context(context_update)

        except (
            pytest_httpchain_engine.substitution.SubstitutionError,
            ValidationError,
            Exception,
        ) as e:
            logger.exception(str(e))
            self._aborted = True
            pytest.fail(reason=str(e), pytrace=False)

    def _prepare_data_context(self, stage_template: Stage, fixture_kwargs: dict[str, Any]) -> dict[str, Any]:
        """Prepare the data context for stage execution."""
        data_context = deepcopy(self.session_manager.get_data_context())
        data_context.update(fixture_kwargs)
        data_context.update(pytest_httpchain_engine.substitution.walk(self.scenario.vars, data_context))
        data_context.update(pytest_httpchain_engine.substitution.walk(stage_template.vars, data_context))
        return data_context

    def _make_http_call(self, stage: Stage, data_context: dict[str, Any]) -> requests.Response:
        """Make the HTTP request for the stage."""
        request_dict = pytest_httpchain_engine.substitution.walk(stage.request, data_context)
        request_model = Request.model_validate(request_dict)
        return RequestHandler.execute(
            session=self.session_manager.get_session(),
            model=request_model,
        )

    def _process_response(self, stage: Stage, response: requests.Response, data_context: dict[str, Any]) -> dict[str, Any]:
        """Process the response and return context updates."""
        context_update: dict[str, Any] = {}
        response_dict = pytest_httpchain_engine.substitution.walk(stage.response, data_context)
        response_model = pytest_httpchain_engine.models.entities.Response.model_validate(response_dict)

        for step in response_model:
            match step:
                case pytest_httpchain_engine.models.entities.SaveStep():
                    save_dict = pytest_httpchain_engine.substitution.walk(step.save, data_context)
                    save_model = Save.model_validate(save_dict)
                    step_update = ResponseHandler.save_data(
                        response=response,
                        model=save_model,
                    )
                    data_context.update(step_update)
                    context_update.update(step_update)

                case pytest_httpchain_engine.models.entities.VerifyStep():
                    verify_dict = pytest_httpchain_engine.substitution.walk(step.verify, data_context)
                    verify_model = Verify.model_validate(verify_dict)
                    VerificationHandler.verify(
                        response=response,
                        model=verify_model,
                        context=data_context,
                    )

        return context_update
