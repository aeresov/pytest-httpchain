import logging
from typing import Any

logger = logging.getLogger(__name__)


class DataContextManager:
    """Manages test data context and state."""

    def __init__(self):
        self._context: dict[str, Any] = {}

    def get(self) -> dict[str, Any]:
        """Get the current data context."""
        return self._context

    def update(self, updates: dict[str, Any]) -> None:
        """Update the data context with new values."""
        self._context.update(updates)

    def clear(self) -> None:
        """Clear all data from the context."""
        self._context.clear()

    def set(self, key: str, value: Any) -> None:
        """Set a specific key in the context."""
        self._context[key] = value

    def get_value(self, key: str, default: Any = None) -> Any:
        """Get a specific value from the context."""
        return self._context.get(key, default)

    def has_key(self, key: str) -> bool:
        """Check if a key exists in the context."""
        return key in self._context

    def remove(self, key: str) -> None:
        """Remove a specific key from the context."""
        if key in self._context:
            del self._context[key]
