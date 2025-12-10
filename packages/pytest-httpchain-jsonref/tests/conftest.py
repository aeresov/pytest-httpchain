"""Shared pytest fixtures for pytest-httpchain-jsonref tests."""

import json
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def create_json_file(tmp_path: Path):
    """Factory fixture for creating temporary JSON files.

    Usage:
        def test_example(create_json_file):
            file = create_json_file("test.json", {"key": "value"})
            result = load_json(file)
    """

    def _create(name: str, content: Any) -> Path:
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(content))
        return file

    return _create


@pytest.fixture
def create_json_files(tmp_path: Path):
    """Factory fixture for creating multiple temporary JSON files at once.

    Usage:
        def test_example(create_json_files):
            files = create_json_files({
                "main.json": {"$ref": "other.json"},
                "other.json": {"value": 42}
            })
            result = load_json(files["main.json"])
    """

    def _create(files: dict[str, Any]) -> dict[str, Path]:
        result = {}
        for name, content in files.items():
            file = tmp_path / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(json.dumps(content))
            result[name] = file
        return result

    return _create
