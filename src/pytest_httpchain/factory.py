"""Collection-time test-class factory.

``create_test_class`` turns a validated `Scenario` into a dynamic pytest test
class — a subclass of `pytest_httpchain.carrier.Carrier` with one
``test NN - <stage name>`` method per stage — seeding the per-scenario class
state the runtime engine operates on. This is the collection half of the
engine: it runs inside ``plugin.JsonModule.collect`` and stays free of side
effects (see `create_test_class` for the one exception), while everything
request-time lives in ``carrier``.
"""

import inspect
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from pytest_httpchain.carrier import Carrier
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
    """Create a dynamic test class from a scenario definition.

    Runs at collection time and stays free of side effects: scenario
    substitutions, ``ssl``/``auth`` resolution, and httpx client construction
    are deferred to ``Carrier._ensure_initialized`` on first stage execution —
    with one exception. Template-bearing stage ``parametrize`` values must be
    resolved NOW (pytest needs concrete parameter values to generate items),
    and they may reference scenario substitutions, so in that case — and only
    that case — the scenario context is resolved at collection and marked as
    such for ``_ensure_initialized`` to reuse.
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
            "client": None,
            "aborted": False,
            "last_request": None,
            "last_response": None,
            "last_exchanges": [],
            "last_iterations_attempted": 0,
            "record_all_exchanges": record_all_exchanges,
            "global_context": base_global_context(scenario_context),
            "_initialized": False,
            "_init_failed": None,
            "_context_resolved_at_collection": needs_collection_context,
            "active_context_managers": [],
            "max_parallel_iterations": max_parallel_iterations,
        },
    )

    total_stages = len(scenario.stages)
    padding_width = len(str(total_stages - 1)) if total_stages > 0 else 1

    for i, stage in enumerate(scenario.stages):
        # Factory captures `stage` by value (as stage_template) per iteration. Do NOT
        # inline this into a closure over the loop variable `stage`: Python closes over
        # the variable, not its value, so every stage method would run the LAST stage.
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
                match step:
                    case IndividualParameter(individual=individual) if individual:
                        param_name = next(iter(individual.keys()))
                        param_values = individual[param_name]
                        resolved_values = walk(param_values, scenario_context)

                        param_ids = step.ids if step.ids else None

                        all_param_names.append(param_name)
                        parametrize_marker = pytest.mark.parametrize(param_name, resolved_values, ids=param_ids)
                        stage_method = parametrize_marker(stage_method)

                    case CombinationsParameter(combinations=combinations) if combinations:
                        resolved_combinations = walk(combinations, scenario_context)
                        resolved_combinations = [vars(item) if isinstance(item, SimpleNamespace) else item for item in resolved_combinations]

                        first_item = resolved_combinations[0]
                        param_names = list(first_item.keys())
                        param_values = [tuple(combo[name] for name in param_names) for combo in resolved_combinations]
                        param_ids = step.ids if step.ids else None

                        all_param_names.extend(param_names)
                        parametrize_marker = pytest.mark.parametrize(",".join(param_names), param_values, ids=param_ids)
                        stage_method = parametrize_marker(stage_method)

                    case _:
                        # New union variant (or a model that no longer satisfies the
                        # guards): fail loudly instead of silently dropping the
                        # parametrization. A plugin bug, so no clean-fail wrapping.
                        raise RuntimeError(f"Unhandled parametrize step: {type(step).__name__}")

        all_fixtures = ["self"] + list(dict.fromkeys(all_param_names + stage.fixtures + scenario.fixtures))
        stage_method.__signature__ = inspect.Signature([inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in all_fixtures])  # ty: ignore[unresolved-attribute]

        # Stage index for the chain-contiguity hook (plugin.pytest_collection_modifyitems),
        # which restores stage order within a class without parsing method names or marks.
        stage_method._httpchain_stage_index = i  # ty: ignore[unresolved-attribute]

        all_marks = [f"order({i})"] + stage.marks
        for mark_str in all_marks:
            try:
                stage_method = make_marker(mark_str)(stage_method)
            except Exception as e:
                # A malformed stage marker is an author error: fail collection (the
                # caller wraps this into a CollectError) instead of silently dropping
                # the marker and running the stage — matching how scenario-level
                # markers are handled in plugin.py.
                raise StageExecutionError(f"Invalid marker '{mark_str}' on stage '{stage.name}': {e}") from e

        method_name = f"test {str(i).zfill(padding_width)} - {stage.name}"
        setattr(CustomCarrier, method_name, stage_method)

    return cast(type[Carrier], CustomCarrier)
