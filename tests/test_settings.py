import pytest
from pydantic import ValidationError

from pytest_http.settings import Settings


def test_settings_default_suffix():
    settings = Settings()
    assert settings.suffix == "http"


@pytest.mark.parametrize("suffix", ["http", "api", "test", "json", "rest", "web_api", "v1", "test-api"])
def test_settings_valid_suffixes(suffix: str):
    settings = Settings(suffix=suffix)
    assert settings.suffix == suffix


@pytest.mark.parametrize(
    "suffix", [".hidden", "api.v2", "test/api", "test api", "test*api", "test|api", "test<api", "test>api", 'test"api', "test'api", "test:api", "test;api", "a" * 33, ""]
)
def test_settings_invalid_suffixes(suffix: str):
    with pytest.raises(ValidationError, match="suffix must"):
        Settings(suffix=suffix)


def test_settings_from_environment(monkeypatch):
    monkeypatch.setenv("SUFFIX", "custom")
    settings = Settings()
    assert settings.suffix == "custom"
