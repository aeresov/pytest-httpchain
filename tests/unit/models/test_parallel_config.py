"""Unit tests for ParallelConfig models."""

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import (
    CombinationsParameter,
    IndividualParameter,
    ParallelForeachConfig,
    ParallelRepeatConfig,
    ParallelThresholds,
    Stage,
)
from tests.unit.models.helpers import assert_error_types, stage_dict


@pytest.mark.parametrize(("attr", "default"), [("max_concurrency", 10), ("calls_per_sec", None), ("max_rate_limit_delay", 60), ("collect_saves", False)])
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
        pytest.param("collect_saves", True, id="collect_saves"),
        pytest.param("collect_saves", "{{ keep_all }}", id="collect_saves-template"),
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


@pytest.mark.parametrize(
    "value",
    [
        # Not set is false; an explicit null is no setting at all.
        pytest.param(None, id="null"),
        pytest.param("yes", id="text"),
        # Template text that is not one complete template.
        pytest.param("keep {{ all }}", id="partial-template"),
        # Of the numbers, only 1 and 0 are a bool.
        pytest.param(2, id="other-number"),
    ],
)
def test_collect_saves_is_a_bool_or_one_template(value):
    with pytest.raises(ValidationError) as exc_info:
        ParallelForeachConfig.model_validate({"foreach": [{"individual": {"n": [1]}}], "collect_saves": value})
    assert_error_types(exc_info, "literal_error", at="collect_saves")


@pytest.mark.parametrize(("value", "flag"), [(1, True), (0, False)])
def test_collect_saves_takes_1_and_0_as_a_bool(value, flag):
    """As every bool setting does (``client.http2``), and as the docs say."""
    config = ParallelForeachConfig.model_validate({"foreach": [{"individual": {"n": [1]}}], "collect_saves": value})
    assert config.collect_saves is flag


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


@pytest.mark.parametrize(("attr", "default"), [("thresholds", None), ("stats_as", None)])
def test_stats_fields_default_to_none(attr, default):
    assert getattr(ParallelRepeatConfig(repeat=5), attr) == default


@pytest.mark.parametrize(
    "thresholds",
    [
        pytest.param({"min_success_ratio": 0}, id="ratio-zero"),
        pytest.param({"min_success_ratio": 1}, id="ratio-one"),
        pytest.param({"min_success_ratio": 0.95, "max_mean_ms": 120, "max_p50_ms": 100, "max_p95_ms": 250.5, "max_p99_ms": 400, "min_rps": 50}, id="all"),
        pytest.param({"min_success_ratio": "{{ ratio }}", "max_p95_ms": "{{ budget }}", "min_rps": "{{ rps }}"}, id="templates"),
        # An explicit null sets no limit, as a retry's max_delay does.
        pytest.param({"max_p95_ms": None}, id="null"),
        pytest.param({}, id="none"),
    ],
)
def test_thresholds_round_trip(thresholds):
    config = ParallelRepeatConfig.model_validate({"repeat": 5, "thresholds": thresholds})
    assert config.thresholds == ParallelThresholds.model_validate(thresholds)
    assert {name: getattr(config.thresholds, name) for name in thresholds} == thresholds


@pytest.mark.parametrize(
    ("thresholds", "field", "error"),
    [
        pytest.param({"min_success_ratio": 1.5}, "min_success_ratio", "less_than_equal", id="ratio-above-one"),
        pytest.param({"min_success_ratio": -0.1}, "min_success_ratio", "greater_than_equal", id="ratio-negative"),
        # A limit no iteration meets, or an infinite one, is not a limit.
        pytest.param({"max_p95_ms": 0}, "max_p95_ms", "greater_than", id="latency-zero"),
        pytest.param({"max_mean_ms": -5}, "max_mean_ms", "greater_than", id="latency-negative"),
        pytest.param({"min_rps": 0}, "min_rps", "greater_than", id="rps-zero"),
        pytest.param({"max_p99_ms": float("inf")}, "max_p99_ms", "finite_number", id="latency-infinite"),
        # Template text that is not one complete template.
        pytest.param({"max_p50_ms": "under {{ budget }}"}, "max_p50_ms", "value_error", id="partial-template"),
        pytest.param({"max_p95": 100}, "max_p95", "extra_forbidden", id="typo"),
    ],
)
def test_thresholds_refused(thresholds, field, error):
    with pytest.raises(ValidationError) as exc_info:
        ParallelRepeatConfig.model_validate({"repeat": 5, "thresholds": thresholds})
    assert_error_types(exc_info, error, at=field)


@pytest.mark.parametrize("value", [True, False])
def test_threshold_refuses_a_bool(value):
    """The number branch reads one as 1 or 0: `true` would demand every
    iteration pass, `false` let a stage whose iterations all failed pass."""
    with pytest.raises(ValidationError) as exc_info:
        ParallelThresholds.model_validate({"min_success_ratio": value})
    assert [(error["loc"], error["msg"]) for error in exc_info.value.errors()] == [
        (("min_success_ratio",), f"Value error, A threshold is a number or a template, got {str(value).lower()}")
    ]


def test_stats_as_is_a_variable_name():
    assert ParallelForeachConfig.model_validate({"foreach": [{"individual": {"n": [1]}}], "stats_as": "load"}).stats_as == "load"


@pytest.mark.parametrize(
    "name",
    [
        # A save's name is never rendered, as a jmespath save's is not.
        pytest.param("{{ name }}", id="template"),
        pytest.param("p95-ms", id="not-an-identifier"),
        pytest.param("class", id="keyword"),
    ],
)
def test_stats_as_refuses_what_is_no_variable_name(name):
    with pytest.raises(ValidationError) as exc_info:
        ParallelRepeatConfig.model_validate({"repeat": 5, "stats_as": name})
    assert_error_types(exc_info, "value_error", at="stats_as")
