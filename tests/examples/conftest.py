import pytest


@pytest.fixture
def string_value():
    return "test_value"


@pytest.fixture
def number_value():
    return 123


@pytest.fixture
def dict_value():
    return {"key": "value", "number": 42}
