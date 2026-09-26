"""Unit tests for SSLConfig model."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import SSLConfig
from tests.unit.models.helpers import assert_error_types


@pytest.mark.parametrize(("attr", "default"), [("verify", True), ("cert", None)])
def test_field_default(attr, default):
    assert getattr(SSLConfig(), attr) == default


@pytest.mark.parametrize(
    ("verify", "expected"),
    [
        pytest.param(True, True, id="true"),
        pytest.param(False, False, id="false"),
        pytest.param("/path/to/ca-bundle.crt", Path("/path/to/ca-bundle.crt"), id="path-str"),
        pytest.param(Path("/path/to/ca-bundle.crt"), Path("/path/to/ca-bundle.crt"), id="path"),
        pytest.param("{{ verify_ssl }}", "{{ verify_ssl }}", id="template"),
        pytest.param("{{ env == 'production' }}", "{{ env == 'production' }}", id="template-conditional"),
    ],
)
def test_verify_accepted(verify, expected):
    """A plain string is a CA bundle path; a template stays a str for later rendering."""
    assert SSLConfig(verify=verify).verify == expected


@pytest.mark.parametrize(
    ("cert", "expected"),
    [
        pytest.param("/path/to/client.pem", Path("/path/to/client.pem"), id="path-str"),
        pytest.param(Path("/path/to/client.pem"), Path("/path/to/client.pem"), id="path"),
        pytest.param(
            ["/path/to/client.crt", "/path/to/client.key"],
            (Path("/path/to/client.crt"), Path("/path/to/client.key")),
            id="pair-json-list",
        ),
        pytest.param(
            (Path("/path/to/client.crt"), Path("/path/to/client.key")),
            (Path("/path/to/client.crt"), Path("/path/to/client.key")),
            id="pair-paths",
        ),
        pytest.param("/path/to/{{ cert_file }}", "/path/to/{{ cert_file }}", id="template"),
        pytest.param(("{{ cert_path }}", "{{ key_path }}"), ("{{ cert_path }}", "{{ key_path }}"), id="pair-templates"),
        pytest.param(
            ("/path/to/{{ client }}.crt", Path("/path/to/client.key")),
            ("/path/to/{{ client }}.crt", Path("/path/to/client.key")),
            id="pair-mixed",
        ),
    ],
)
def test_cert_accepted(cert, expected):
    """A single combined PEM or a (cert, key) pair; template parts stay str."""
    assert SSLConfig(cert=cert).cert == expected


@pytest.mark.parametrize(
    ("cert", "error_type"),
    [
        pytest.param(["/path/cert.crt", "/path/key.key", "/extra.pem"], "too_long", id="three"),
        pytest.param(["/path/only.pem"], "missing", id="one"),
    ],
)
def test_cert_pair_must_have_two_items(cert, error_type):
    with pytest.raises(ValidationError) as exc_info:
        SSLConfig(cert=cert)
    assert_error_types(exc_info, error_type, at="cert")
