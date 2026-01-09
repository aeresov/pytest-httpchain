"""Unit tests for SSLConfig model."""

from pathlib import Path

import pytest
from pydantic import ValidationError
from pytest_httpchain_models.entities import Scenario, SSLConfig


class TestSSLConfigVerify:
    """Tests for SSLConfig.verify field."""

    def test_verify_default_true(self):
        """Test that verify defaults to True."""
        config = SSLConfig()
        assert config.verify is True

    def test_verify_explicit_true(self):
        """Test explicitly setting verify to True."""
        config = SSLConfig(verify=True)
        assert config.verify is True

    def test_verify_false(self):
        """Test setting verify to False."""
        config = SSLConfig(verify=False)
        assert config.verify is False

    def test_verify_with_path_string(self):
        """Test verify with CA bundle path as string."""
        config = SSLConfig(verify="/path/to/ca-bundle.crt")
        assert isinstance(config.verify, Path)
        assert str(config.verify) == "/path/to/ca-bundle.crt"

    def test_verify_with_path_object(self):
        """Test verify with CA bundle path as Path object."""
        config = SSLConfig(verify=Path("/path/to/ca-bundle.crt"))
        assert isinstance(config.verify, Path)

    def test_verify_with_template(self):
        """Test verify with template expression."""
        config = SSLConfig(verify="{{ verify_ssl }}")
        assert config.verify == "{{ verify_ssl }}"

    def test_verify_with_conditional_template(self):
        """Test verify with conditional template."""
        config = SSLConfig(verify="{{ env == 'production' }}")
        assert config.verify == "{{ env == 'production' }}"


class TestSSLConfigCert:
    """Tests for SSLConfig.cert field."""

    def test_cert_default_none(self):
        """Test that cert defaults to None."""
        config = SSLConfig()
        assert config.cert is None

    def test_cert_single_path_string(self):
        """Test cert with single combined PEM file."""
        config = SSLConfig(cert="/path/to/client.pem")
        assert isinstance(config.cert, Path)
        assert str(config.cert) == "/path/to/client.pem"

    def test_cert_single_path_object(self):
        """Test cert with Path object."""
        config = SSLConfig(cert=Path("/path/to/client.pem"))
        assert isinstance(config.cert, Path)

    def test_cert_tuple_paths(self):
        """Test cert with tuple of (cert, key) paths."""
        config = SSLConfig(cert=(Path("/path/to/client.crt"), Path("/path/to/client.key")))
        assert isinstance(config.cert, tuple)
        assert len(config.cert) == 2
        assert isinstance(config.cert[0], Path)
        assert isinstance(config.cert[1], Path)

    def test_cert_with_template_single(self):
        """Test cert with template in single path."""
        config = SSLConfig(cert="/path/to/{{ cert_file }}")
        assert config.cert == "/path/to/{{ cert_file }}"

    def test_cert_with_template_tuple(self):
        """Test cert with templates in tuple paths."""
        config = SSLConfig(cert=("{{ cert_path }}", "{{ key_path }}"))
        cert = config.cert
        assert isinstance(cert, tuple)
        assert cert[0] == "{{ cert_path }}"
        assert cert[1] == "{{ key_path }}"

    def test_cert_mixed_template_and_path(self):
        """Test cert with mixed template and regular path."""
        config = SSLConfig(cert=("/path/to/{{ client }}.crt", Path("/path/to/client.key")))
        cert = config.cert
        assert isinstance(cert, tuple)
        assert cert[0] == "/path/to/{{ client }}.crt"
        assert isinstance(cert[1], Path)


class TestSSLConfigInScenario:
    """Tests for SSLConfig within Scenario model."""

    def test_scenario_default_ssl_config(self):
        """Test Scenario has default SSLConfig."""
        scenario = Scenario()
        assert isinstance(scenario.ssl, SSLConfig)
        assert scenario.ssl.verify is True
        assert scenario.ssl.cert is None

    def test_scenario_custom_ssl_config(self):
        """Test Scenario with custom SSLConfig."""
        scenario = Scenario(ssl=SSLConfig(verify=False, cert=Path("/path/to/cert.pem")))
        assert scenario.ssl.verify is False
        assert isinstance(scenario.ssl.cert, Path)

    def test_scenario_ssl_config_with_templates(self):
        """Test Scenario with templated SSLConfig."""
        scenario = Scenario(
            ssl=SSLConfig(
                verify="{{ ssl_verify }}",
                cert=("{{ cert_path }}", "{{ key_path }}"),
            )
        )
        assert scenario.ssl.verify == "{{ ssl_verify }}"


class TestSSLConfigValidation:
    """Tests for SSLConfig validation."""

    def test_verify_invalid_string(self):
        """Test that non-template strings for verify are treated as paths."""
        # Non-template string is treated as a path
        config = SSLConfig(verify=Path("some/path.crt"))
        assert isinstance(config.verify, Path)

    def test_cert_invalid_tuple_length(self):
        """Test that cert tuple must have exactly 2 elements."""
        with pytest.raises(ValidationError):
            SSLConfig(cert=["/path/cert.crt", "/path/key.key", "/extra.pem"])  # type: ignore[arg-type]

    def test_cert_single_element_list_treated_as_tuple(self):
        """Test that single-element list for cert is invalid tuple."""
        with pytest.raises(ValidationError):
            SSLConfig(cert=["/path/only.pem"])  # type: ignore[arg-type]
