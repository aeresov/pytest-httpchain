"""Unit tests for a stage's RetryConfig."""

import pytest
from pydantic import ValidationError

from pytest_httpchain.models import RETRY_ON, RetryConfig, Stage
from tests.unit.models.helpers import assert_error_types, stage_dict


def test_defaults():
    """One second between attempts, no backoff, no cap, and every kind of
    failure that another attempt may change."""
    config = RetryConfig(attempts=3)
    assert (config.delay, config.backoff, config.max_delay, config.on) == (1.0, 1.0, None, list(RETRY_ON))


def test_default_on_is_not_shared():
    first, second = RetryConfig(attempts=2), RetryConfig(attempts=2)
    first.on.append("verify")
    assert second.on == list(RETRY_ON)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("attempts", 1, id="attempts-one"),
        pytest.param("attempts", "{{ max_polls }}", id="attempts-template"),
        pytest.param("delay", 0.0, id="delay-zero"),
        pytest.param("delay", "{{ first_wait }}", id="delay-template"),
        pytest.param("backoff", 1.0, id="backoff-one"),
        pytest.param("backoff", "{{ factor }}", id="backoff-template"),
        pytest.param("max_delay", 0.0, id="max-delay-zero"),
        pytest.param("max_delay", "{{ cap }}", id="max-delay-template"),
        # One kind, or a list of them, as a header matcher's contains takes
        # one pattern or a list.
        pytest.param("on", "verify", id="on-one-kind"),
        pytest.param("on", ["save", "request"], id="on-list"),
    ],
)
def test_field_round_trip(field, value):
    assert getattr(RetryConfig.model_validate({"attempts": 2, field: value}), field) == value


@pytest.mark.parametrize(
    ("config", "error", "field"),
    [
        pytest.param({}, "missing", "attempts", id="attempts-required"),
        pytest.param({"attempts": 0}, "greater_than", "attempts", id="attempts-zero"),
        pytest.param({"attempts": 2.5}, "int_from_float", "attempts", id="attempts-fractional"),
        pytest.param({"attempts": 2, "delay": -0.5}, "greater_than_equal", "delay", id="delay-negative"),
        # Below 1, each wait would be shorter than the last.
        pytest.param({"attempts": 2, "backoff": 0.5}, "greater_than_equal", "backoff", id="backoff-shrinking"),
        pytest.param({"attempts": 2, "max_delay": -1}, "greater_than_equal", "max_delay", id="max-delay-negative"),
        # What JSON's 1e999 and NaN read as, which the carrier refuses: at
        # load, `validate` does too.
        pytest.param({"attempts": 2, "delay": float("inf")}, "finite_number", "delay", id="delay-infinite"),
        pytest.param({"attempts": 2, "backoff": float("inf")}, "finite_number", "backoff", id="backoff-infinite"),
        pytest.param({"attempts": 2, "max_delay": float("inf")}, "finite_number", "max_delay", id="max-delay-infinite"),
        pytest.param({"attempts": 2, "delay": float("nan")}, "finite_number", "delay", id="delay-nan"),
        # A bool would be read as 1, which turns retry off or waits a second
        # without a word: refused ahead of the union, which takes it (lax mode).
        pytest.param({"attempts": True}, "value_error", "attempts", id="attempts-bool"),
        pytest.param({"attempts": 2, "delay": True}, "value_error", "delay", id="delay-bool"),
        pytest.param({"attempts": 2, "backoff": False}, "value_error", "backoff", id="backoff-bool"),
        pytest.param({"attempts": 2, "max_delay": True}, "value_error", "max_delay", id="max-delay-bool"),
        # A template is a whole value: text around it is no number.
        pytest.param({"attempts": "{{ n }} times"}, "value_error", "attempts", id="attempts-partial-template"),
        pytest.param({"attempts": 2, "on": "timeout"}, "literal_error", "on", id="on-unknown-kind"),
        # Retrying on nothing is a retry that never retries.
        pytest.param({"attempts": 2, "on": []}, "too_short", "on", id="on-empty"),
        pytest.param({"attempts": 2, "max_attempts": 3}, "extra_forbidden", "max_attempts", id="unknown-key"),
    ],
)
def test_invalid(config, error, field):
    with pytest.raises(ValidationError) as exc_info:
        RetryConfig.model_validate(config)
    assert_error_types(exc_info, error, at=field)


def test_bool_refused_in_one_sentence():
    """Once, not once per member of the union, and saying what goes there."""
    with pytest.raises(ValidationError) as exc_info:
        RetryConfig.model_validate({"attempts": True})
    assert [(error["loc"], error["msg"]) for error in exc_info.value.errors()] == [(("attempts",), "Value error, A retry setting is a number or a template, got true")]


def test_stage_takes_it():
    stage = Stage.model_validate(stage_dict(retry={"attempts": 10, "delay": 0.5, "backoff": 2, "max_delay": 5, "on": ["verify", "save", "request"]}))
    assert stage.retry == RetryConfig(attempts=10, delay=0.5, backoff=2, max_delay=5)


def test_stage_without_it():
    assert Stage.model_validate(stage_dict()).retry is None
