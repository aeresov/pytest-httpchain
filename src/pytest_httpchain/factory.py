"""Collection-time test-class factory: a validated `Scenario` becomes a
`Carrier` subclass with one ``test NN - <stage name>`` method per stage."""

import inspect
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from pytest_httpchain.carrier import Carrier, fresh_scenario_state
from pytest_httpchain.errors import StageExecutionError
from pytest_httpchain.models import (
    CombinationsParameter,
    IndividualParameter,
    Scenario,
    Stage,
    parametrize_values_contain_template,
)
from pytest_httpchain.scoping import base_global_context
from pytest_httpchain.templates import walk
from pytest_httpchain.utils import make_marker, process_substitutions


def create_test_class(
    scenario: Scenario,
    class_name: str,
    max_parallel_iterations: int = 10_000,
    scenario_dir: Path | None = None,
    record_all_exchanges: bool = False,
) -> type[Carrier]:
    """Build a scenario's test class.

    Free of side effects — substitutions, ssl/auth and the client are deferred
    to `Carrier._ensure_initialized` — with one exception: template-bearing
    parametrize values must resolve now (pytest needs concrete values to
    generate items), so those scenarios resolve their context here and mark it
    for reuse.
    """
    needs_collection_context = any(parametrize_values_contain_template(stage.parametrize) for stage in scenario.stages)
    scenario_context = process_substitutions(scenario.substitutions) if needs_collection_context else {}

    CustomCarrier = type(
        class_name,
        (Carrier,),
        {
            "__doc__": scenario.description,
            "scenario": scenario,
            "scenario_dir": scenario_dir,
            "record_all_exchanges": record_all_exchanges,
            "global_context": base_global_context(scenario_context),
            "_context_resolved_at_collection": needs_collection_context,
            "max_parallel_iterations": max_parallel_iterations,
            # Mutable per-run state (client, abort flag, exchange bookkeeping):
            # owned per scenario, defined once in carrier.
            **fresh_scenario_state(),
        },
    )

    total_stages = len(scenario.stages)
    padding_width = len(str(total_stages - 1)) if total_stages > 0 else 1

    for i, stage in enumerate(scenario.stages):
        # Captures `stage` by value: a closure over the loop variable would make
        # every method run the LAST stage.
        def make_stage_method(stage_template: Stage) -> Callable:
            def call_execute_stage(self, **kwargs):
                type(self).execute_stage(stage_template, kwargs)

            return call_execute_stage

        stage_method = make_stage_method(stage)

        if stage.description:
            stage_method.__doc__ = stage.description

        all_param_names = []

        if stage.parametrize:
            for step in stage.parametrize:
                # Each step reduces to pytest.mark.parametrize's (argnames, argvalues).
                match step:
                    case IndividualParameter(individual=individual) if individual:
                        param_names = [next(iter(individual))]
                        param_values = walk(individual[param_names[0]], scenario_context)

                    case CombinationsParameter(combinations=combinations) if combinations:
                        resolved_combinations = [vars(item) if isinstance(item, SimpleNamespace) else item for item in walk(combinations, scenario_context)]
                        param_names = list(resolved_combinations[0].keys())
                        param_values = [tuple(combo[name] for name in param_names) for combo in resolved_combinations]

                    case _:
                        raise RuntimeError(f"Unhandled parametrize step: {type(step).__name__}")

                all_param_names.extend(param_names)
                stage_method = pytest.mark.parametrize(",".join(param_names), param_values, ids=step.ids or None)(stage_method)

        all_fixtures = ["self"] + list(dict.fromkeys(all_param_names + stage.fixtures + scenario.fixtures))
        stage_method.__signature__ = inspect.Signature([inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in all_fixtures])  # ty: ignore[unresolved-attribute]

        # Read by the chain-contiguity hook to restore stage order.
        stage_method._httpchain_stage_index = i  # ty: ignore[unresolved-attribute]

        all_marks = [f"order({i})"] + stage.marks
        for mark_str in all_marks:
            try:
                stage_method = make_marker(mark_str)(stage_method)
            except Exception as e:
                # An author error: fail collection rather than silently drop the
                # marker (the caller wraps this into a CollectError).
                raise StageExecutionError(f"Invalid marker '{mark_str}' on stage '{stage.name}': {e}") from e

        method_name = f"test {str(i).zfill(padding_width)} - {stage.name}"
        setattr(CustomCarrier, method_name, stage_method)

    return cast(type[Carrier], CustomCarrier)
