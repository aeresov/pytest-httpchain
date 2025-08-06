import logging
from typing import Any

import requests
from pytest_httpchain_engine.models.entities import Scenario

from pytest_httpchain.core.data_context import DataContextManager
from pytest_httpchain.core.session_manager import SessionManager

logger = logging.getLogger(__name__)


class HTTPSessionManager:
    """Facade that combines session management and data context handling.

    This class maintains backward compatibility while delegating
    responsibilities to specialized managers.
    """

    def __init__(self, scenario: Scenario):
        self.scenario = scenario
        self._session_manager = SessionManager(scenario)
        self._data_context_manager = DataContextManager()

    def setup(self) -> None:
        """Initialize the HTTP session with SSL and authentication configuration."""
        self._session_manager.setup(self._data_context_manager.get())

    def teardown(self) -> None:
        """Clean up the HTTP session and context."""
        self._data_context_manager.clear()
        self._session_manager.teardown()

    def get_session(self) -> requests.Session:
        """Get the current HTTP session."""
        return self._session_manager.get()

    def get_data_context(self) -> dict[str, Any]:
        """Get the current data context."""
        return self._data_context_manager.get()

    def update_data_context(self, updates: dict[str, Any]) -> None:
        """Update the data context with new values."""
        self._data_context_manager.update(updates)

    @property
    def session_manager(self) -> SessionManager:
        """Direct access to the session manager."""
        return self._session_manager

    @property
    def data_context_manager(self) -> DataContextManager:
        """Direct access to the data context manager."""
        return self._data_context_manager
