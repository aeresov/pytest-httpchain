"""Unit tests for ParallelConfig models."""

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import (
    CombinationsParameter,
    IndividualParameter,
    ParallelForeachConfig,
    ParallelRepeatConfig,
    Stage,
)
from tests.unit.models.helpers import assert_error_types, stage_dict


@pytest.mark.parametrize(("attr", "default"), [("max_concurrency", 10), ("calls_per_sec", None), ("max_rate_limit_delay", 60)])
def test_base_field_default(attr, default):
    assert getattr(ParallelRepeatConfig(repeat=5), attr) == default


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("repeat", 100, id="repeat"),
        pytest.param("repeat", "{{ repeat_count }}", id="repeat-template"),
        pytest.param("max_concurrency", 5, id="max_concurrency"),
        pytest.param("max_concurrency", "{{ max_workers }}", id="max_concurrency-template"),
        pytest.param("calls_per_sec", 10, id="calls_per_sec"),
        pytest.param("calls_per_sec", "{{ rate_limit }}", id="calls_per_sec-template"),
    ],
)
def test_repeat_field_round_trip(field, value):
    assert getattr(ParallelRepeatConfig(**{"repeat": 10, field: value}), field) == value


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        pytest.param({"repeat": 0}, "repeat", id="repeat-zero"),
        pytest.param({"repeat": -1}, "repeat", id="repeat-negative"),
        pytest.param({"repeat": 10, "max_concurrency": 0}, "max_concurrency", id="max_concurrency-zero"),
    ],
)
def test_counts_must_be_positive(kwargs, field):
    with pytest.raises(ValidationError) as exc_info:
        ParallelRepeatConfig(**kwargs)
    assert_error_types(exc_info, "greater_than", at=field)


def test_foreach_takes_raw_parameter_steps():
    config = ParallelForeachConfig.model_validate(
        {"foreach": [{"individual": {"id": [1, 2, 3]}}, {"combinations": [{"method": "GET", "path": "/a"}, {"method": "POST", "path": "/b"}]}]}
    )
    assert config.foreach == [
        IndividualParameter(individual={"id": [1, 2, 3]}),
        CombinationsParameter(combinations=[{"method": "GET", "path": "/a"}, {"method": "POST", "path": "/b"}]),
    ]


def test_empty_foreach_rejected():
    """An empty foreach is meaningless: at runtime it would silently run the
    request once, unparameterized. Reject it at the model layer."""
    with pytest.raises(ValidationError) as exc_info:
        ParallelForeachConfig(foreach=[])
    assert_error_types(exc_info, "too_short", at="foreach")


@pytest.mark.parametrize(
    ("parallel", "expected"),
    [
        pytest.param({"repeat": 100}, ParallelRepeatConfig, id="repeat"),
        pytest.param({"foreach": [{"individual": {"id": [1, 2, 3]}}]}, ParallelForeachConfig, id="foreach"),
    ],
)
def test_raw_parallel_dict_selects_model(parallel, expected):
    assert type(Stage.model_validate(stage_dict(parallel=parallel)).parallel) is expected
