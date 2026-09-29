"""Collection-time test-class factory: a validated `Scenario` becomes a
`Carrier` subclass with one ``test NN - <stage name>`` method per stage."""

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from pytest_httpchain.body_schema import UNBOUNDED, ReferenceBounds
from pytest_httpchain.carrier import Carrier, fresh_scenario_state
from pytest_httpchain.errors import StageExecutionError
from pytest_httpchain.models import (
    CombinationsParameter,
    IndividualParameter,
    Scenario,
    Stage,
    parametrize_values_contain_template,
)
from pytest_httpchain.redaction import DEFAULT_REDACTION, Redaction
from pytest_httpchain.scoping import base_global_context
from pytest_httpchain.templates import walk
from pytest_httpchain.utils import make_marker, process_substitutions


def _make_stage_method(stage_template: Stage) -> Callable:
    """Build one stage's test method.

    The stage arrives as an argument, not captured from the caller's loop
    variable: a closure over that variable would make every method run the LAST
    stage.
    """

    def call_execute_stage(self, **kwargs):
        type(self).execute_stage(stage_template, kwargs)

    return call_execute_stage


def _parametrize_values(stage: Stage, field: str, value: Any) -> list[Any]:
    """A template-form parametrize step's re-validated values, or a collection
    error naming the step.

    Both step kinds also accept a template string, so a template rendering to
    another template passes re-validation as text. Unchecked, pytest would
    parametrize over its characters (``individual``), and reading the first
    combination's keys would fail with a bare AttributeError
    (``combinations``). ``parallel.foreach`` guards the same case at run time
    (``carrier``).
    """
    if not isinstance(value, list):
        raise StageExecutionError(f"parametrize {field} on stage '{stage.name}' must resolve to a list, got {value!r}")
    return value


def create_test_class(
    scenario: Scenario,
    class_name: str,
    max_parallel_iterations: int = 10_000,
    scenario_dir: Path | None = None,
    ref_bounds: ReferenceBounds = UNBOUNDED,
    record_all_exchanges: bool = False,
    redaction: Redaction = DEFAULT_REDACTION,
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
            "ref_bounds": ref_bounds,
            "record_all_exchanges": record_all_exchanges,
            "redaction": redaction,
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
        stage_method = _make_stage_method(stage)

        if stage.description:
            stage_method.__doc__ = stage.description

        all_param_names = []

        if stage.parametrize:
            for step in stage.parametrize:
                # Each step reduces to pytest.mark.parametrize's (argnames, argvalues).
                # The template forms of both parameter kinds skip their model's
                # own checks ("values/keys unknown until runtime"), and nothing
                # re-validates the resolved value — so they are fed back through
                # the model here. Without it, heterogeneous combinations silently
                # drop the keys missing from the first one, or fail with a bare
                # KeyError naming neither the index nor the problem.
                match step:
                    case IndividualParameter(individual=individual) if individual:
                        param_names = [next(iter(individual))]
                        declared_values = individual[param_names[0]]
                        param_values = walk(declared_values, scenario_context)
                        if isinstance(declared_values, str):
                            revalidated = IndividualParameter.model_validate({"individual": {param_names[0]: param_values}, "ids": step.ids})
                            param_values = _parametrize_values(stage, f"individual '{param_names[0]}'", revalidated.individual[param_names[0]])

                    case CombinationsParameter(combinations=combinations) if combinations:
                        resolved_combinations = walk(combinations, scenario_context)
                        if isinstance(combinations, str):
                            revalidated_combos = CombinationsParameter.model_validate({"combinations": resolved_combinations, "ids": step.ids})
                            resolved_combinations = _parametrize_values(stage, "combinations", revalidated_combos.combinations)
                        param_names = list(resolved_combinations[0].keys())
                        # pytest unpacks each argvalue only for several argnames:
                        # a lone key takes the bare value, or its 1-tuple would
                        # reach the request as "(1,)".
                        if len(param_names) == 1:
                            param_values = [combo[param_names[0]] for combo in resolved_combinations]
                        else:
                            param_values = [tuple(combo[name] for name in param_names) for combo in resolved_combinations]

                    case _:
                        raise RuntimeError(f"Unhandled parametrize step: {type(step).__name__}")

                all_param_names.extend(param_names)
                stage_method = pytest.mark.parametrize(",".join(param_names), param_values, ids=step.ids or None)(stage_method)

        all_fixtures = ["self", *dict.fromkeys(all_param_names + stage.fixtures + scenario.fixtures)]
        stage_method.__signature__ = inspect.Signature([inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in all_fixtures])  # ty: ignore[unresolved-attribute]

        # Read by the chain-contiguity hook to restore stage order.
        stage_method._httpchain_stage_index = i  # ty: ignore[unresolved-attribute]

        # The generated name ("test NN - <stage>") satisfies pytest's default
        # `python_functions` only via its bare "test" prefix rule — the space
        # defeats every glob form, so a narrowed `python_functions = test_*`
        # would collect ZERO stages and leave CI green. __test__ is honored by
        # pytest's istestfunction regardless of the name filter.
        stage_method.__test__ = True  # ty: ignore[unresolved-attribute]

        for mark_str in stage.marks:
            try:
                stage_method = make_marker(mark_str)(stage_method)
            except Exception as e:
                # An author error: fail collection rather than silently drop the
                # marker (the caller wraps this into a CollectError).
                raise StageExecutionError(f"Invalid marker '{mark_str}' on stage '{stage.name}': {e}") from e

        method_name = f"test {str(i).zfill(padding_width)} - {stage.name}"
        setattr(CustomCarrier, method_name, stage_method)

    return cast(type[Carrier], CustomCarrier)
