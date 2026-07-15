"""Test fixtures for the userfunc tests."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _helpers_on_syspath(monkeypatch):
    """Make this directory importable so tests can load helper modules via
    ``import_function("userfunc_test_helpers:func_name")``.

    An autouse fixture with ``monkeypatch.syspath_prepend`` (scoped, auto-undone)
    instead of a module-level ``sys.path.insert``, so the path does not leak
    into the rest of a full-suite run.
    """
    monkeypatch.syspath_prepend(str(Path(__file__).parent))
