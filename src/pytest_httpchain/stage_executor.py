"""Stage execution logic for HTTP chain tests.

This module orchestrates the execution of individual test stages by coordinating
between the specialized modules:

- context.py: Handles context preparation and management
- request.py: Manages HTTP request building and execution
- response.py: Processes responses (save and verify operations)
- exceptions.py: Defines the exception hierarchy

The main responsibility of this module is to coordinate the flow:
1. Build local context using context module
2. Prepare and execute request using request module
3. Process response using response module
4. Return updates for global context
"""

import logging
from typing import Any

import pytest_httpchain_templates.substitution
import requests
from pytest_httpchain_models.entities import (
    Request,
    Response,
    Save,
    SaveStep,
    Scenario,
    Stage,
    Verify,
    VerifyStep,
)

from .context import prepare_data_context
from .request import prepare_and_execute
from .response import process_save_step, process_verify_step

logger = logging.getLogger(__name__)


def execute_stage(
    stage_template: Stage,
    scenario: Scenario,
    session: requests.Session,
    global_context: dict[str, Any],
    fixture_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Execute a single stage and return context updates.

    This is the main entry point for stage execution. It orchestrates:
    1. Context preparation (merge global + fixtures + variables)
    2. Template substitution for all stage elements
    3. HTTP request preparation and execution
    4. Response processing (save and verify steps)
    5. Return updates for global context

    Args:
        stage_template: The stage definition (with templates)
        scenario: The scenario configuration
        session: HTTP session for requests
        global_context: Shared context from previous stages (read-only)
        fixture_kwargs: Values from pytest fixtures

    Returns:
        Context updates to be merged into global context.
        Only includes variables from SaveStep operations.

    Raises:
        RequestError: HTTP request preparation/execution failed
        ResponseError: Response processing (save) failed
        VerificationError: Response verification failed

    Note:
        The function maintains a clear separation between global and local
        context. Only SaveStep results are returned for global updates.
    """
    # Build local context for this stage (global + fixtures + vars)
    local_context = prepare_data_context(scenario=scenario, stage_template=stage_template, global_context=global_context, fixture_kwargs=fixture_kwargs)

    # Resolve stage template with complete local context
    stage = pytest_httpchain_templates.substitution.walk(stage_template, local_context)

    # Prepare and execute request
    request_dict = pytest_httpchain_templates.substitution.walk(stage.request, local_context)
    request_model = Request.model_validate(request_dict)
    response = prepare_and_execute(session, request_model)

    # Process response
    response_dict = pytest_httpchain_templates.substitution.walk(stage.response, local_context)
    response_model = Response.model_validate(response_dict)

    # Track what needs to be saved to global context
    global_context_updates: dict[str, Any] = {}

    for step in response_model:
        match step:
            case SaveStep():
                save_dict = pytest_httpchain_templates.substitution.walk(step.save, local_context)
                save_model = Save.model_validate(save_dict)
                saved_vars = process_save_step(save_model, response)
                # Add saved vars as a new layer in ChainMap for subsequent steps
                local_context = local_context.new_child(saved_vars)
                global_context_updates.update(saved_vars)

            case VerifyStep():
                verify_dict = pytest_httpchain_templates.substitution.walk(step.verify, local_context)
                verify_model = Verify.model_validate(verify_dict)
                process_verify_step(verify_model, local_context, response)

    # Return only the updates that should persist globally
    return global_context_updates
