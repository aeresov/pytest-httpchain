def test_simple():
    assert 1 + 1 == 2


def test_with_fixture(string_value):
    assert string_value == "test_value"
