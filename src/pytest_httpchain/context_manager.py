"""Centralized context management for HTTP chain test execution.

This module provides a ContextManager class that handles all context-related
operations including variable caching, fixture processing, and data context preparation.
"""

import logging
from collections import ChainMap
from typing import Any

import pytest_httpchain_templates.substitution
from pytest_httpchain_models.entities import Scenario, Stage

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

    def __init__(self):
        """Initialize the context manager."""
        self.global_context: dict[str, Any] = {}
        self.scenario_vars_cache: dict[str, Any] | None = None
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

        # Build base context
        ChainMap(stage_fixtures, self.global_context)

        # Process scenario variables (cached after first evaluation)
        if self.scenario_vars_cache is None and scenario.vars:
            # Use cached fixtures for scenario vars to ensure consistency
            scenario_context = ChainMap(self.scenario_fixtures_cache, self.global_context)
            self.scenario_vars_cache = pytest_httpchain_templates.substitution.walk(
                scenario.vars,
                scenario_context,
            )
            for scenario_var_name in scenario.vars.keys():
                logger.info(f"Seeded {scenario_var_name} = {self.scenario_vars_cache[scenario_var_name]}")

        scenario_vars = self.scenario_vars_cache or {}

        # Process stage variables (fresh for each stage)
        stage_vars = {}
        if stage.vars:
            # Build context with scenario vars included
            stage_context = ChainMap({}, scenario_vars, stage_fixtures, self.global_context)

            # Process stage vars incrementally so they can reference each other
            for key, value in stage.vars.items():
                resolved_value = pytest_httpchain_templates.substitution.walk(value, stage_context)
                stage_vars[key] = resolved_value
                # Add to context for next vars to reference
                stage_context.maps[0][key] = resolved_value
                logger.info(f"Seeded {key} = {resolved_value}")

        # Return final layered context
        # Precedence: stage_vars > scenario_vars > fixtures > global
        return ChainMap(stage_vars, scenario_vars, stage_fixtures, self.global_context)

    def update_global_context(self, updates: dict[str, Any]):
        """Update the global context with new values.

        Args:
            updates: Dictionary of values to add to global context
        """
        self.global_context.update(updates)

    def cleanup(self):
        """Clean up any active resources."""
        self.fixture_manager.cleanup()
