"""Shared fixtures for tests."""

import pytest


@pytest.fixture
def sample_fixture():
    return "Hello from fixture!"


@pytest.fixture
def another_fixture():
    return {"data": "Another fixture value", "number": 42}
