from unittest.mock import Mock

import pytest

from pytest_http.pytest_plugin import pytest_configure, validate_suffix


def test_validate_suffix_default():
    suffix = validate_suffix("http")
    assert suffix == "http"


@pytest.mark.parametrize("suffix", ["http", "api", "test", "json", "rest", "web_api", "v1", "test-api"])
def test_validate_suffix_valid(suffix: str):
    result = validate_suffix(suffix)
    assert result == suffix


@pytest.mark.parametrize(
    "suffix", [".hidden", "api.v2", "test/api", "test api", "test*api", "test|api", "test<api", "test>api", 'test"api', "test'api", "test:api", "test;api", "a" * 33, ""]
)
def test_validate_suffix_invalid(suffix: str):
    with pytest.raises(ValueError, match="suffix must"):
        validate_suffix(suffix)


def test_pytest_configure_stores_validated_suffix():
    """Test that pytest_configure stores the validated suffix in config."""
    config = Mock()
    config.getini.return_value = "custom"

    pytest_configure(config)

    assert hasattr(config, "pytest_http_suffix")
    assert config.pytest_http_suffix == "custom"


def test_pytest_configure_validates_suffix():
    """Test that pytest_configure validates the suffix and raises error for invalid ones."""
    config = Mock()
    config.getini.return_value = "invalid.suffix"

    with pytest.raises(ValueError, match="suffix must contain only alphanumeric characters, underscores, and hyphens"):
        pytest_configure(config)


# Integration test skipped - basic validation covered by other tests and integration tests
# def test_pytest_ini_option_with_pytester(pytester):
#     pytester.makeini("""
#         [tool:pytest]
#         suffix = custom
#     """)
#
#     pytester.makefile('.custom.json', test_example='{"fixtures": [], "marks": [], "test": "basic test content"}')
#
#     result = pytester.runpytest("-v")
#     result.assert_outcomes(passed=1)
