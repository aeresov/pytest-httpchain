"""Unified fixture management for HTTP chain tests.

This module provides a manager for handling all types of pytest fixtures:
- Plain values (strings, numbers, objects)
- Factory functions that return values
- Factory functions that return context managers
"""

import contextlib
import inspect
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any


class FixtureManager:
    """Manages all fixtures (plain values and factories) during test execution.

    This manager handles:
    - Plain fixture values (passed through as-is)
    - Factory functions (wrapped to be callable in templates)
    - Context managers returned by factories (tracked for cleanup)
    """

    def __init__(self):
        self.active_contexts: list[contextlib.AbstractContextManager] = []
        self.wrapped_fixtures: dict[str, Any] = {}

    def process_fixtures(self, fixture_kwargs: dict[str, Any]) -> dict[str, Any]:
        """Process all fixtures and prepare them for use in templates.

        Args:
            fixture_kwargs: Raw fixture values from pytest

        Returns:
            Dictionary of processed fixtures ready for template context
        """
        processed = {}

        for name, value in fixture_kwargs.items():
            if callable(value) and not inspect.isclass(value):
                processed[name] = self._wrap_factory(name, value)
            else:
                processed[name] = value

        self.wrapped_fixtures = processed
        return processed

    def _wrap_factory(self, name: str, factory: Callable) -> Callable:
        """Wrap a factory function to handle context managers automatically.

        Args:
            name: Name of the fixture
            factory: The factory function

        Returns:
            A wrapped factory that handles context managers
        """

        def wrapped(*args, **kwargs):
            result = factory(*args, **kwargs)

            is_context_manager = isinstance(result, AbstractContextManager) or (hasattr(result, "__enter__") and hasattr(result, "__exit__"))
            if is_context_manager:
                value = result.__enter__()
                self.active_contexts.append(result)
                return value

            return result

        return wrapped

    def cleanup(self):
        """Clean up all active context managers."""
        while self.active_contexts:
            ctx = self.active_contexts.pop()
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass  # Log but don't fail cleanup

        self.wrapped_fixtures.clear()
