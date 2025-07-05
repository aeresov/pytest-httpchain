from unittest.mock import Mock

from pytest_http.pytest_plugin import get_test_name_pattern


def create_mock_config(suffix="http"):
    """Create a mock config object with the given suffix."""
    config = Mock()
    config.getini.return_value = suffix
    config.pytest_http_suffix = suffix  # Simulate what pytest_configure sets
    return config


def test_pattern_matches_valid_files():
    pattern = get_test_name_pattern(create_mock_config())
    assert pattern.match("test_example.http.json")
    assert pattern.match("test_user_login.http.json")
    assert pattern.match("test_api_endpoint.http.json")
    assert pattern.match("test_123.http.json")
    assert pattern.match("test_.http.json")


def test_pattern_extracts_name():
    pattern = get_test_name_pattern(create_mock_config())
    match = pattern.match("test_example.http.json")
    assert match.group("name") == "example"

    match = pattern.match("test_user_login.http.json")
    assert match.group("name") == "user_login"

    match = pattern.match("test_123.http.json")
    assert match.group("name") == "123"


def test_pattern_does_not_match_invalid_files():
    pattern = get_test_name_pattern(create_mock_config())
    assert not pattern.match("example.http.json")
    assert not pattern.match("test_example.json")
    assert not pattern.match("test_example.http")
    assert not pattern.match("not_test_example.http.json")
    assert not pattern.match("test.http.json")
    assert not pattern.match("TEST_example.http.json")


def test_pattern_is_case_sensitive():
    pattern = get_test_name_pattern(create_mock_config())
    assert not pattern.match("Test_example.http.json")
    assert not pattern.match("test_Example.HTTP.json")
    assert not pattern.match("test_example.Http.Json")


def test_pattern_with_underscores_in_name():
    pattern = get_test_name_pattern(create_mock_config())
    test_cases = [
        ("test_a_b_c.http.json", "a_b_c"),
        ("test_user_auth_flow.http.json", "user_auth_flow"),
        ("test___multiple___underscores___.http.json", "__multiple___underscores___"),
        ("test_trailing_.http.json", "trailing_"),
        ("test__leading.http.json", "_leading"),
    ]

    for filename, expected_name in test_cases:
        match = pattern.match(filename)
        assert match is not None, f"Pattern should match {filename}"
        assert match.group("name") == expected_name


def test_pattern_with_numbers():
    pattern = get_test_name_pattern(create_mock_config())
    test_cases = [
        ("test_123.http.json", "123"),
        ("test_api_v2.http.json", "api_v2"),
        ("test_2023_data.http.json", "2023_data"),
        ("test_v1_0_0.http.json", "v1_0_0"),
    ]

    for filename, expected_name in test_cases:
        match = pattern.match(filename)
        assert match is not None, f"Pattern should match {filename}"
        assert match.group("name") == expected_name


def test_pattern_with_special_but_valid_characters():
    pattern = get_test_name_pattern(create_mock_config())
    test_cases = [
        ("test_api-endpoint.http.json", "api-endpoint"),
        ("test_data.backup.http.json", "data.backup"),
    ]

    for filename, expected_name in test_cases:
        match = pattern.match(filename)
        assert match is not None, f"Pattern should match {filename}"
        assert match.group("name") == expected_name


def test_pattern_with_custom_suffix():
    pattern = get_test_name_pattern(create_mock_config("api"))

    assert pattern.match("test_example.api.json") is not None
    assert pattern.match("test_complex_name.api.json") is not None

    assert pattern.match("test_example.http.json") is None
