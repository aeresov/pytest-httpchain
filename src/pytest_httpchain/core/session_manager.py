import logging
from typing import Any

import pytest_httpchain_engine.substitution
import requests
from pytest_httpchain_engine.functions import AuthFunction
from pytest_httpchain_engine.models.entities import Scenario, UserFunctionKwargs

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages HTTP session lifecycle and configuration."""

    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self._session: requests.Session | None = None

    def setup(self, data_context: dict[str, Any] | None = None) -> None:
        """Initialize the HTTP session with SSL and authentication configuration."""
        self._session = requests.Session()
        self._configure_ssl()
        self._configure_auth(data_context or {})

    def teardown(self) -> None:
        """Clean up the HTTP session."""
        if self._session:
            self._session.close()
            self._session = None

    def get(self) -> requests.Session:
        """Get the current HTTP session."""
        if self._session is None:
            raise RuntimeError("Session not initialized. Call setup() first.")
        return self._session

    def is_initialized(self) -> bool:
        """Check if the session has been initialized."""
        return self._session is not None

    def _configure_ssl(self) -> None:
        """Configure SSL settings for the session."""
        if self._session is None:
            return

        self._session.verify = self.scenario.ssl.verify
        if self.scenario.ssl.cert is not None:
            self._session.cert = self.scenario.ssl.cert

    def _configure_auth(self, data_context: dict[str, Any]) -> None:
        """Configure authentication for the session."""
        if self._session is None or not self.scenario.auth:
            return

        resolved_auth = pytest_httpchain_engine.substitution.walk(self.scenario.auth, data_context)

        match resolved_auth:
            case str():
                auth_instance = AuthFunction.call(resolved_auth)
            case UserFunctionKwargs():
                auth_instance = AuthFunction.call_with_kwargs(resolved_auth.function, resolved_auth.kwargs)

        self._session.auth = auth_instance
