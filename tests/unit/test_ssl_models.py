"""Unit tests for SSL configuration models and validation."""

import pytest
from pydantic import ValidationError
from pytest_http_engine.models import Request, Scenario, SSLConfig
from pytest_http_engine.types import validate_ssl_cert_path, validate_ssl_verify_path


class TestSSLPathValidators:
    """Test SSL path validation functions."""

    def test_validate_ssl_verify_path_valid(self):
        """Test valid SSL verify paths (files and directories)."""
        valid_paths = [
            "/etc/ssl/certs",  # Directory
            "/etc/ssl/certs/ca-bundle.crt",  # File
            "./certs",  # Relative directory
            "./certs/ca-bundle.crt",  # Relative file
            "C:\\certs",  # Windows directory
            "C:\\certs\\ca-bundle.crt",  # Windows file
            "ca-bundle.crt",  # Simple filename
            ".hidden-certs",  # Hidden directory/file
        ]

        for path in valid_paths:
            result = validate_ssl_verify_path(path)
            assert result == path

    def test_validate_ssl_verify_path_invalid(self):
        """Test invalid SSL verify paths."""
        with pytest.raises(ValueError, match="SSL verify path cannot be empty"):
            validate_ssl_verify_path("")

    def test_validate_ssl_cert_path_valid(self):
        """Test valid SSL certificate paths (files only)."""
        valid_paths = [
            "/path/to/client.crt",  # Certificate file
            "/path/to/client.key",  # Key file
            "/path/to/client.pem",  # PEM file
            "./certs/client.crt",  # Relative path
            "C:\\certs\\client.pfx",  # Windows path
            "client.pem",  # Simple filename
            ".client-cert",  # Hidden file
        ]

        for path in valid_paths:
            result = validate_ssl_cert_path(path)
            assert result == path

    def test_validate_ssl_cert_path_invalid(self):
        """Test invalid SSL certificate paths."""
        with pytest.raises(ValueError, match="SSL certificate path cannot be empty"):
            validate_ssl_cert_path("")


class TestSSLConfig:
    """Test SSLConfig model."""

    def test_ssl_config_verify_boolean(self):
        """Test SSL config with boolean verify values."""
        # Test verify=True
        config = SSLConfig(verify=True)
        assert config.verify is True
        assert config.cert is None

        # Test verify=False
        config = SSLConfig(verify=False)
        assert config.verify is False
        assert config.cert is None

    def test_ssl_config_verify_path(self):
        """Test SSL config with verify path."""
        config = SSLConfig(verify="/etc/ssl/certs/ca-bundle.crt")
        assert config.verify == "/etc/ssl/certs/ca-bundle.crt"

    def test_ssl_config_cert_single_file(self):
        """Test SSL config with single certificate file."""
        config = SSLConfig(cert="/path/to/client.pem")
        assert config.cert == "/path/to/client.pem"

    def test_ssl_config_cert_tuple(self):
        """Test SSL config with certificate tuple."""
        config = SSLConfig(cert=("/path/to/client.crt", "/path/to/client.key"))
        assert config.cert == ("/path/to/client.crt", "/path/to/client.key")

    def test_ssl_config_combined(self):
        """Test SSL config with both verify and cert."""
        config = SSLConfig(verify="/etc/ssl/certs", cert=("/path/to/client.crt", "/path/to/client.key"))
        assert config.verify == "/etc/ssl/certs"
        assert config.cert == ("/path/to/client.crt", "/path/to/client.key")

    def test_ssl_config_defaults(self):
        """Test SSL config with default values."""
        config = SSLConfig()
        assert config.verify is True  # Default value
        assert config.cert is None

    def test_ssl_config_verify_empty_path(self):
        """Test SSL config with empty verify path."""
        with pytest.raises(ValidationError) as exc_info:
            SSLConfig(verify="")
        assert "SSL verify path cannot be empty" in str(exc_info.value)

    def test_ssl_config_cert_empty_path(self):
        """Test SSL config with empty cert path."""
        with pytest.raises(ValidationError) as exc_info:
            SSLConfig(cert="")
        assert "SSL certificate path cannot be empty" in str(exc_info.value)

    def test_ssl_config_cert_empty_in_tuple(self):
        """Test SSL config with empty cert path in tuple."""
        with pytest.raises(ValidationError) as exc_info:
            SSLConfig(cert=("/path/to/client.crt", ""))
        assert "SSL certificate path cannot be empty" in str(exc_info.value)

    def test_ssl_config_cert_invalid_tuple_length(self):
        """Test SSL config with invalid tuple length."""
        with pytest.raises(ValidationError):
            SSLConfig(cert=("/path/to/client.crt",))  # Only one element

        with pytest.raises(ValidationError):
            SSLConfig(cert=("/path/to/client.crt", "/path/to/client.key", "/extra"))  # Three elements


class TestScenarioSSL:
    """Test Scenario model with SSL configuration."""

    def test_scenario_with_ssl(self):
        """Test scenario with SSL configuration."""
        scenario = Scenario(ssl=SSLConfig(verify="/etc/ssl/certs", cert=("/path/to/client.crt", "/path/to/client.key")))
        assert scenario.ssl.verify == "/etc/ssl/certs"
        assert scenario.ssl.cert == ("/path/to/client.crt", "/path/to/client.key")

    def test_scenario_without_ssl(self):
        """Test scenario without SSL configuration."""
        scenario = Scenario()
        assert scenario.ssl is None

    def test_scenario_ssl_from_dict(self):
        """Test scenario SSL configuration from dictionary."""
        scenario_data = {"ssl": {"verify": False, "cert": "/path/to/client.pem"}, "flow": []}
        scenario = Scenario.model_validate(scenario_data)
        assert scenario.ssl.verify is False
        assert scenario.ssl.cert == "/path/to/client.pem"

    def test_scenario_ssl_with_cert_tuple_from_list(self):
        """Test scenario SSL with cert tuple from JSON list."""
        scenario_data = {"ssl": {"verify": "/etc/ssl/certs", "cert": ["/path/to/client.crt", "/path/to/client.key"]}, "flow": []}
        scenario = Scenario.model_validate(scenario_data)
        assert scenario.ssl.verify == "/etc/ssl/certs"
        assert scenario.ssl.cert == ("/path/to/client.crt", "/path/to/client.key")


class TestRequestSSL:
    """Test Request model with SSL configuration."""

    def test_request_with_ssl(self):
        """Test request with SSL configuration."""
        request = Request(url="https://api.example.com/test", ssl=SSLConfig(verify=False, cert="/path/to/client.pem"))
        assert request.ssl.verify is False
        assert request.ssl.cert == "/path/to/client.pem"

    def test_request_without_ssl(self):
        """Test request without SSL configuration."""
        request = Request(url="https://api.example.com/test")
        assert request.ssl is None

    def test_request_ssl_from_dict(self):
        """Test request SSL configuration from dictionary."""
        request_data = {"url": "https://api.example.com/test", "ssl": {"verify": True, "cert": ["/path/to/client.crt", "/path/to/client.key"]}}
        request = Request.model_validate(request_data)
        assert request.ssl.verify is True
        assert request.ssl.cert == ("/path/to/client.crt", "/path/to/client.key")


class TestSSLSerialization:
    """Test SSL configuration serialization and deserialization."""

    def test_ssl_config_model_dump(self):
        """Test SSL config model dump."""
        config = SSLConfig(verify="/etc/ssl/certs", cert=("/path/to/client.crt", "/path/to/client.key"))
        data = config.model_dump()
        assert data["verify"] == "/etc/ssl/certs"
        assert data["cert"] == ("/path/to/client.crt", "/path/to/client.key")

    def test_scenario_ssl_model_dump(self):
        """Test scenario with SSL model dump."""
        scenario = Scenario(ssl=SSLConfig(verify=False, cert="/path/to/client.pem"), flow=[])
        data = scenario.model_dump(exclude_unset=True)
        assert data["ssl"]["verify"] is False
        assert data["ssl"]["cert"] == "/path/to/client.pem"

    def test_request_ssl_model_dump(self):
        """Test request with SSL model dump."""
        request = Request(url="https://api.example.com/test", ssl=SSLConfig(verify=True, cert=("/client.crt", "/client.key")))
        data = request.model_dump(exclude_unset=True)
        assert data["ssl"]["verify"] is True
        assert data["ssl"]["cert"] == ("/client.crt", "/client.key")

    def test_roundtrip_serialization(self):
        """Test roundtrip serialization of SSL configuration."""
        original_scenario = Scenario(ssl=SSLConfig(verify="/custom/ca-bundle.crt", cert=("/path/to/client.crt", "/path/to/client.key")), flow=[])

        # Serialize to dict
        data = original_scenario.model_dump()

        # Deserialize back to model
        restored_scenario = Scenario.model_validate(data)

        # Check that values are preserved
        assert restored_scenario.ssl.verify == "/custom/ca-bundle.crt"
        assert restored_scenario.ssl.cert == ("/path/to/client.crt", "/path/to/client.key")
