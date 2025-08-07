from typing import Any, Protocol, runtime_checkable

import requests
from requests.auth import AuthBase


@runtime_checkable
class VerifyFunction(Protocol):
    """Protocol for HTTP response verification functions."""

    def __call__(self, response: requests.Response, **kwargs: Any) -> bool:
        """Verify an HTTP response.

        Args:
            response: The HTTP response to verify
            **kwargs: Additional context/parameters

        Returns:
            True if verification passes, False otherwise
        """
        ...


@runtime_checkable
class AuthFunction(Protocol):
    """Protocol for authentication functions that return AuthBase."""

    def __call__(self, **kwargs: Any) -> AuthBase:
        """Create an authentication object.

        Args:
            **kwargs: Authentication parameters

        Returns:
            AuthBase instance for request authentication
        """
        ...


@runtime_checkable
class SaveFunction(Protocol):
    """Protocol for save/extraction functions that return dict."""

    def __call__(self, response: requests.Response, **kwargs: Any) -> dict[str, Any]:
        """Extract and save data from a response.

        Args:
            response: The HTTP response to process
            **kwargs: Additional parameters

        Returns:
            Dictionary of extracted data to save to context
        """
        ...
