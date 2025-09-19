"""Centralized context management for HTTP chain test execution.

This module provides a ContextManager class that handles all context-related
operations including variable caching, fixture processing, and data context preparation.
"""

import logging
from collections import ChainMap
from typing import Any

import pytest_httpchain_templates.substitution
from pytest_httpchain_models.entities import Scenario, Stage, UserFunctionKwargs, UserFunctionName
from pytest_httpchain_userfunc.userfunc import wrap_function

from .fixture_manager import FixtureManager

logger = logging.getLogger(__name__)


class ContextManager:
    """Manages all context-related operations for test execution.

    This class centralizes:
    - Global data context shared across stages
    - Scenario variable caching (evaluated once per test class)
    - Fixture caching for scenario variables
    - Context preparation for each stage
    """

    def __init__(self, seed_context: dict[str, Any] | None = None):
        """Initialize the context manager with optional seed context.

        Args:
            seed_context: Pre-processed context from scenario substitutions containing
                         both wrapped functions and resolved variables
        """
        self.global_context: dict[str, Any] = {}
        self.seed_context: dict[str, Any] = seed_context or {}
        self.scenario_fixtures_cache: dict[str, Any] | None = None
        self.fixture_manager = FixtureManager()

    def prepare_stage_context(
        self,
        scenario: Scenario,
        stage: Stage,
        fixture_kwargs: dict[str, Any],
    ) -> ChainMap[str, Any]:
        """Prepare the complete data context for stage execution.

        This method handles:
        - Processing fixtures (caching for scenario vars, fresh for stage vars)
        - Evaluating scenario variables (once and cached)
        - Evaluating stage variables (fresh for each stage)
        - Building the layered context with proper precedence

        Args:
            scenario: The scenario configuration
            stage: The stage being executed
            fixture_kwargs: Raw pytest fixture values for this stage

        Returns:
            ChainMap with layered context for the stage
        """
        # Process and cache fixtures for scenario vars on first call
        if self.scenario_fixtures_cache is None:
            self.scenario_fixtures_cache = self.fixture_manager.process_fixtures(fixture_kwargs)

        # Process fixtures fresh for stage vars
        stage_fixtures = self.fixture_manager.process_fixtures(fixture_kwargs)

        # Process stage substitutions (fresh for each stage)
        stage_vars = {}
        for step in stage.substitutions:
            # Build context for this substitution step with seed context having priority over fixtures and global
            # Context: stage_vars > seed_context > fixtures > global
            stage_context = ChainMap(stage_vars, self.seed_context, stage_fixtures, self.global_context)

            if step.functions:
                for alias, func_def in step.functions.items():
                    match func_def:
                        case UserFunctionName():
                            stage_vars[alias] = wrap_function(func_def.root)
                        case UserFunctionKwargs():
                            stage_vars[alias] = wrap_function(func_def.name.root, default_kwargs=func_def.kwargs)
                        case _:
                            raise RuntimeError(f"Invalid function definition for '{alias}': expected UserFunctionName or UserFunctionKwargs")
                    logger.info(f"Seeded {alias} = {stage_vars[alias]} (function)")

            if step.vars:
                for key, value in step.vars.items():
                    resolved_value = pytest_httpchain_templates.substitution.walk(value, stage_context)
                    stage_vars[key] = resolved_value
                    logger.info(f"Seeded {key} = {resolved_value}")

        # Return final layered context
        # Precedence: stage_vars > seed_context > fixtures > global
        return ChainMap(stage_vars, self.seed_context, stage_fixtures, self.global_context)

    def update_global_context(self, updates: dict[str, Any]):
        """Update the global context with new values.

        Args:
            updates: Dictionary of values to add to global context
        """
        self.global_context.update(updates)

    def cleanup(self):
        """Clean up any active resources."""
        self.fixture_manager.cleanup()
