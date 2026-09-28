"""errors.py: which stage failures a stage's ``retry`` may retry by default."""

import pytest

from pytest_httpchain.errors import RequestError, SaveError, StageExecutionError, VerificationError


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        # A save or a verify step failing on this response: the next may differ.
        pytest.param(SaveError, True, id="save"),
        pytest.param(VerificationError, True, id="verify"),
        # A request error is retryable only where the network failed it,
        # which the carrier says where it raises one.
        pytest.param(RequestError, False, id="request"),
        pytest.param(StageExecutionError, False, id="stage"),
    ],
)
def test_default(error, retryable):
    assert error("failed").retryable is retryable


@pytest.mark.parametrize("error", [SaveError, VerificationError, RequestError, StageExecutionError])
@pytest.mark.parametrize("retryable", [True, False])
def test_raise_site_overrides_the_default(error, retryable):
    assert error("failed", retryable=retryable).retryable is retryable


def test_first_attempt_by_default():
    error = VerificationError("failed")
    assert (error.attempt, error.earlier_exchanges) == (1, ())
