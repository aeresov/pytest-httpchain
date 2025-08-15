"""Factory for creating dynamic test classes.

This module provides functionality to dynamically generate pytest test classes
from JSON scenario definitions. Each scenario becomes a test class with one
test method per stage.
"""

import inspect
import logging
from typing import Any

import pytest
from pytest_httpchain_models.entities import Scenario, Stage
from simpleeval import EvalWithCompoundTypes

from .carrier import Carrier

logger = logging.getLogger(__name__)


def create_test_class(scenario: Scenario, class_name: str) -> type[Carrier]:
    """Create a dynamic test class for the given scenario.

    This factory function generates a pytest test class with:
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
        >>> TestClass = create_test_class(scenario, Path("test.json"), "TestAPI")
        >>> # TestClass will have methods: test_0_stage1, test_1_stage2, etc.
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
        def stage_method(self, *, _stage: Stage = stage, **fixture_kwargs: dict[str, Any]) -> None:
            """Execute a single stage of the test scenario.

            Auto-generated method that executes one stage of the HTTP chain test.

            Args:
                **fixture_kwargs: Pytest fixtures requested by this stage
            """
            CustomCarrier.execute_stage(_stage, fixture_kwargs)

        # Set up method signature with fixtures
        all_fixtures: list[str] = ["self"] + stage.fixtures + scenario.fixtures
        stage_method.__signature__ = inspect.Signature([inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in all_fixtures])

        # Apply markers
        all_marks: list[str] = [f"order({i})"] + stage.marks
        evaluator = EvalWithCompoundTypes(names={"pytest": pytest})
        for mark_str in all_marks:
            try:
                marker = evaluator.eval(f"pytest.mark.{mark_str}")
                if marker:
                    stage_method = marker(stage_method)
            except Exception as e:
                logger.warning(f"Failed to create marker '{mark_str}': {e}")

        # Add method to class with descriptive name
        method_name = f"test_{i}_{stage.name}"
        setattr(CustomCarrier, method_name, stage_method)

    return CustomCarrier
