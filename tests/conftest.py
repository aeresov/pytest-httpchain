"""Shared fixtures for tests."""

import pytest


@pytest.fixture
def sample_fixture():
    return "Hello from fixture!"


@pytest.fixture
def another_fixture():
    return {"data": "Another fixture value", "number": 42}


@pytest.fixture
def base_value():
    return 15


@pytest.fixture
def multiplier():
    return 2


@pytest.fixture
def test_name():
    return "calculation"


@pytest.fixture
def enabled():
    return True


@pytest.fixture
def config():
    return {"server": "localhost", "port": 8080}
