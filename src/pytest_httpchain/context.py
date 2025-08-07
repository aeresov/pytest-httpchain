"""Context management for HTTP chain test execution.

This module handles the preparation and management of execution contexts,
maintaining the separation between global and local state.
"""

from collections import ChainMap
from typing import Any

import pytest_httpchain_templates.substitution
from pytest_httpchain_models.entities import Scenario, Stage


def prepare_data_context(
    scenario: Scenario,
    stage_template: Stage,
    global_context: dict[str, Any],
    fixture_kwargs: dict[str, Any],
) -> ChainMap[str, Any]:
    """Prepare the complete data context for stage execution.

    Uses ChainMap for efficient layered context management with lazy evaluation.
    No copying occurs - all layers share references to original data.

    Merges contexts in order of precedence (later overrides earlier):
    1. Global context (shared across all stages) - base layer
    2. Fixture values (from pytest fixtures)
    3. Scenario variables (from scenario.vars)
    4. Stage variables (from stage.vars) - top layer

    Each level can reference variables from previous levels in templates.

    Args:
        scenario: The scenario configuration
        stage_template: The stage being executed
        global_context: Shared context from previous stages
        fixture_kwargs: Pytest fixture values for this stage

    Returns:
        ChainMap with layered context for efficient lookups

    Note:
        Returns a ChainMap for full performance benefits:
        - No data copying
        - Lazy evaluation (only accesses what's needed)
        - Memory efficient (shares references)
        - O(1) for most lookups
    """
    # Build layers incrementally - each layer can reference previous ones
    # Template substitution now works directly with ChainMap

    # Layer 1: Base context (global + fixtures)
    base_context = ChainMap(fixture_kwargs, global_context)

    # Layer 2: Scenario variables (can reference base)
    scenario_vars = {}
    if scenario.vars:
        scenario_vars = pytest_httpchain_templates.substitution.walk(
            scenario.vars,
            base_context,  # Pass ChainMap directly
        )

    # Layer 3: Stage variables (can reference base + scenario)
    # Process stage vars incrementally so they can reference each other
    stage_vars = {}
    if stage_template.vars:
        context_with_scenario = ChainMap({}, scenario_vars, fixture_kwargs, global_context)
        for key, value in stage_template.vars.items():
            resolved_value = pytest_httpchain_templates.substitution.walk(value, context_with_scenario)
            stage_vars[key] = resolved_value
            # Add resolved var to context so next vars can reference it
            context_with_scenario.maps[0][key] = resolved_value

    # Create final context with proper precedence order
    # Stage vars override scenario vars, which override fixtures, which override global
    # Returns ChainMap for full performance benefits
    return ChainMap(stage_vars, scenario_vars, fixture_kwargs, global_context)
