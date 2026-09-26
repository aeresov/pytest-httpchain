"""Shared pytest fixtures for the jsonref tests.

Note: the ``datadir`` fixture used by some of these tests is NOT defined here.
It is provided by the third-party ``pytest-datadir`` plugin (a dev dependency),
which copies the per-module data directory (``<test_module_name>/``) into a
temporary location and yields its path.
"""

import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def create_json_file(tmp_path: Path):
    """Factory: ``create_json_file("sub/a.json", content)`` writes ``content``
    as JSON under ``tmp_path`` (creating directories) and returns the path."""

    def _create(name: str, content: Any) -> Path:
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(content))
        return file

    return _create


@pytest.fixture
def create_json_files(create_json_file):
    """Factory: ``create_json_files({name: content, ...})`` writes each file and
    returns ``{name: path}``."""

    def _create(files: dict[str, Any]) -> dict[str, Path]:
        return {name: create_json_file(name, content) for name, content in files.items()}

    return _create
