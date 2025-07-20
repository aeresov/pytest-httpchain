import pytest
from pydantic import ValidationError
from pytest_http_engine.models import Request


def test_request_timeout_valid():
    """Test that valid timeout values are accepted."""
    # Float timeout
    request = Request(url="http://example.com", timeout=30.5)
    assert request.timeout == 30.5

    # Integer timeout (will be converted to float)
    request = Request(url="http://example.com", timeout=10)
    assert request.timeout == 10.0

    # No timeout
    request = Request(url="http://example.com")
    assert request.timeout is None


def test_request_timeout_invalid():
    """Test that invalid timeout values are rejected."""
    # Negative timeout
    with pytest.raises(ValidationError) as exc_info:
        Request(url="http://example.com", timeout=-1)
    assert "greater than 0" in str(exc_info.value)

    # Zero timeout
    with pytest.raises(ValidationError) as exc_info:
        Request(url="http://example.com", timeout=0)
    assert "greater than 0" in str(exc_info.value)
