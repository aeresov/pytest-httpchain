import pytest


@pytest.fixture
def string_value():
    return "test_value"


@pytest.fixture
def int_value():
    return 42
