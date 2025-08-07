"""Factory for creating dynamic test classes."""

import inspect
import logging
from pathlib import Path
from typing import Any

import pytest
from pytest_httpchain_models.entities import Scenario
from simpleeval import EvalWithCompoundTypes

from .carrier import Carrier

logger = logging.getLogger(__name__)


def create_test_class(scenario: Scenario, module_path: Path, class_name: str) -> type:
    """
    Create a dynamic test class for the given scenario.

    This handles:
    - Creating a Carrier subclass with the scenario
    - Adding test methods for each stage
    - Applying markers to methods

    Args:
        scenario: The test scenario to execute
        module_path: Path to the module (for reference)
        class_name: Name for the test class

    Returns:
        A dynamically created test class
    """
    # Create custom Carrier class with scenario bound
    CustomCarrier = type(
        class_name,
        (Carrier,),
        {
            "_scenario": scenario,
            "_session": None,
            "_data_context": {},
            "_aborted": False,
        },
    )

    # Add stage methods dynamically
    for i, stage in enumerate(scenario.stages):
        # Create stage method - using default argument to capture stage
        def stage_method(self, *, _stage=stage, **fixture_kwargs: Any):
            CustomCarrier.execute_stage(_stage, fixture_kwargs)

        # Set up method signature with fixtures
        all_fixtures = ["self"] + stage.fixtures + scenario.fixtures
        stage_method.__signature__ = inspect.Signature([inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in all_fixtures])

        # Apply markers
        all_marks = [f"order({i})"] + stage.marks
        evaluator = EvalWithCompoundTypes(names={"pytest": pytest})
        for mark_str in all_marks:
            try:
                marker = evaluator.eval(f"pytest.mark.{mark_str}")
                if marker:
                    stage_method = marker(stage_method)
            except Exception as e:
                logger.warning(f"Failed to create marker '{mark_str}': {e}")

        setattr(CustomCarrier, f"test_{i}_{stage.name}", stage_method)

    return CustomCarrier
