"""Test fixtures and helper functions for pytest-httpchain-userfunc tests.

Functions defined here are used to test conftest lookup functionality.
"""

import sys
from pathlib import Path

# Add tests directory to sys.path so `importlib.import_module("conftest")` works
_tests_dir = Path(__file__).parent
if str(_tests_dir) not in sys.path:
    sys.path.insert(0, str(_tests_dir))


def conftest_helper(x: int, y: int) -> int:
    """A simple helper function for testing conftest imports."""
    return x + y


def conftest_no_args() -> str:
    """Helper function with no arguments."""
    return "conftest_result"


def conftest_with_kwargs(*, name: str = "default") -> str:
    """Helper function with keyword-only arguments."""
    return f"hello, {name}"


# Non-callable for testing error cases
conftest_not_callable = "I am a string, not a function"
