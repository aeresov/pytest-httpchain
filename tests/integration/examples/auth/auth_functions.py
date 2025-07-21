"""Authentication functions for testing."""

from requests.auth import HTTPBasicAuth


def basic_auth():
    """Returns HTTP Basic Auth for user:pass"""
    return HTTPBasicAuth('user', 'pass')


def basic_auth_with_args(username, password):
    """Returns HTTP Basic Auth with provided username and password"""
    return HTTPBasicAuth(username, password)


def invalid_auth():
    """Returns invalid auth type for testing error handling"""
    return "not an AuthBase instance"
