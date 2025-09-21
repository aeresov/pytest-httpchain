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
        processed = {}

        for name, value in fixture_kwargs.items():
            if callable(value) and not inspect.isclass(value):
                processed[name] = self._wrap_factory(name, value)
            else:
                processed[name] = value

        self.wrapped_fixtures = processed
        return processed

    def _wrap_factory(self, name: str, factory: Callable) -> Callable:
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
        while self.active_contexts:
            ctx = self.active_contexts.pop()
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass  # Log but don't fail cleanup

        self.wrapped_fixtures.clear()
