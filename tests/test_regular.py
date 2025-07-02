"""Regular Python tests to show pytest still works normally."""


def test_simple():
    assert 1 + 1 == 2


def test_with_fixture(sample_fixture):
    assert sample_fixture == "Hello from fixture!"
