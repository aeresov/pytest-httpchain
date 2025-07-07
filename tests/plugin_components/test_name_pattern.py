import pytest

from pytest_http.pytest_plugin import get_test_name_pattern


class SimpleConfig:
    """Simple config object for testing."""
    def __init__(self, suffix="http"):
        self.pytest_http_suffix = suffix


@pytest.mark.parametrize(
    "filename,expected_name",
    [
        ("test_example.http.json", "example"),
        ("test_user_login.http.json", "user_login"),
        ("test_123.http.json", "123"),
        ("test_api_endpoint.http.json", "api_endpoint"),
        ("test_a_b_c.http.json", "a_b_c"),
        ("test_user_auth_flow.http.json", "user_auth_flow"),
        ("test___multiple___underscores___.http.json", "__multiple___underscores___"),
        ("test_trailing_.http.json", "trailing_"),
        ("test__leading.http.json", "_leading"),
        ("test_api_v2.http.json", "api_v2"),
        ("test_2023_data.http.json", "2023_data"),
        ("test_v1_0_0.http.json", "v1_0_0"),
        ("test_api-endpoint.http.json", "api-endpoint"),
        ("test_data.backup.http.json", "data.backup"),
    ],
)
def test_pattern_matches_and_extracts_name(filename, expected_name):
    pattern = get_test_name_pattern(SimpleConfig())
    match = pattern.match(filename)
    assert match is not None
    assert match.group("name") == expected_name


@pytest.mark.parametrize(
    "filename",
    [
        "example.http.json",
        "test_example.json",
        "test_example.http",
        "not_test_example.http.json",
        "test.http.json",
        "test_.http.json",
        "TEST_example.http.json",
        "Test_example.http.json",
        "test_Example.HTTP.json",
        "test_example.Http.Json",
    ],
)
def test_pattern_does_not_match_invalid_files(filename):
    pattern = get_test_name_pattern(SimpleConfig())
    assert not pattern.match(filename)


def test_pattern_is_case_sensitive():
    pattern = get_test_name_pattern(SimpleConfig())
    assert not pattern.match("Test_example.http.json")
    assert not pattern.match("test_Example.HTTP.json")
    assert not pattern.match("test_example.Http.Json")


def test_pattern_with_special_but_valid_characters():
    pattern = get_test_name_pattern(SimpleConfig())
    test_cases = [
        ("test_api-endpoint.http.json", "api-endpoint"),
        ("test_data.backup.http.json", "data.backup"),
    ]

    for filename, expected_name in test_cases:
        match = pattern.match(filename)
        assert match is not None, f"Pattern should match {filename}"
        assert match.group("name") == expected_name


def test_pattern_with_custom_suffix():
    pattern = get_test_name_pattern(SimpleConfig("api"))

    assert pattern.match("test_example.api.json") is not None
    assert pattern.match("test_complex_name.api.json") is not None
    assert pattern.match("test_example.http.json") is None
