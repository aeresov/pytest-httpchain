import os
from unittest.mock import Mock, patch

import pytest
from pytest_http_engine.models import AWSCredentials, AWSProfile
from requests_auth_aws_sigv4 import AWSSigV4

from pytest_http.plugin import create_aws_auth

pytest.importorskip("boto3")
pytest.importorskip("requests_auth_aws_sigv4")


def test_create_aws_auth_with_profile():
    with patch("boto3.Session") as mock_session:
        mock_credentials = Mock()
        mock_credentials.access_key = "test_access_key"
        mock_credentials.secret_key = "test_secret_key"
        mock_credentials.token = "test_token"

        mock_session_instance = Mock()
        mock_session_instance.get_credentials.return_value = mock_credentials
        mock_session.return_value = mock_session_instance

        aws_config = AWSProfile(service="execute-api", region="us-west-2", profile="test-profile")

        auth = create_aws_auth(aws_config)

        assert isinstance(auth, AWSSigV4)
        assert mock_session.called


def test_create_aws_auth_with_credentials():
    aws_config = AWSCredentials(
        service="s3",
        region="us-east-1",
        access_key_id="test_access_key",
        secret_access_key="test_secret_key",
        session_token="test_token",
    )

    auth = create_aws_auth(aws_config)

    assert isinstance(auth, AWSSigV4)


def test_create_aws_auth_with_credentials_no_session_token():
    aws_config = AWSCredentials(
        service="lambda",
        region="eu-west-1",
        access_key_id="test_access_key",
        secret_access_key="test_secret_key",
    )

    auth = create_aws_auth(aws_config)

    assert isinstance(auth, AWSSigV4)


def test_create_aws_auth_profile_no_credentials():
    with patch("boto3.Session") as mock_session:
        mock_session_instance = Mock()
        mock_session_instance.get_credentials.return_value = None
        mock_session.return_value = mock_session_instance

        aws_config = AWSProfile(service="execute-api", region="us-west-2", profile="invalid-profile")

        with pytest.raises(ValueError, match="Could not get credentials for AWS profile 'invalid-profile'"):
            create_aws_auth(aws_config)


def test_create_aws_auth_unavailable_imports():
    with patch("builtins.__import__", side_effect=ImportError("No module named 'boto3'")):
        aws_config = AWSProfile(service="execute-api", region="us-west-2", profile="test-profile")

        with pytest.raises(ImportError):
            create_aws_auth(aws_config)


def test_aws_profile_env_defaults():
    with patch.dict(os.environ, {"AWS_PROFILE": "env-profile", "AWS_DEFAULT_REGION": "env-region"}):
        aws_config = AWSProfile(service="execute-api")

        assert aws_config.profile == "env-profile"
        assert aws_config.region == "env-region"


def test_aws_credentials_env_defaults():
    with patch.dict(
        os.environ,
        {
            "AWS_ACCESS_KEY_ID": "env_access_key",
            "AWS_SECRET_ACCESS_KEY": "env_secret_key",
            "AWS_SESSION_TOKEN": "env_token",
            "AWS_DEFAULT_REGION": "env-region",
        },
    ):
        aws_config = AWSCredentials(service="s3")

        assert aws_config.access_key_id == "env_access_key"
        assert aws_config.secret_access_key == "env_secret_key"
        assert aws_config.session_token == "env_token"
        assert aws_config.region == "env-region"
