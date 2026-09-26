"""Builders for scenarios written inline by an integration test, and table rows.

A plain helpers module (not conftest.py, which pytest treats as a plugin file,
not an import target)::

    from tests.integration.helpers import har_entries, named, stage
"""

import json
from pathlib import Path
from typing import Any

import pytest


def stage(name: str, path: str = "/ok", *, request: dict[str, Any] | None = None, response: list[Any] | None = None, **fields: Any) -> dict[str, Any]:
    """One stage against the example conftest's ``server`` fixture.

    It GETs ``{{ server }}<path>`` and verifies a 200 unless ``response`` says
    otherwise. ``request`` entries extend the request (method, body, timeout,
    ...) and ``fields`` add stage keys (parallel, substitutions, ...), so a call
    site spells out only what its test is about.
    """
    return {
        "name": name,
        "fixtures": ["server"],
        "request": {"url": "{{ server }}" + path, **(request or {})},
        "response": [{"verify": {"status": 200}}] if response is None else response,
        **fields,
    }


def write_scenario(directory: Path, scenario: dict[str, Any], name: str = "test_inline.http.json") -> Path:
    path = directory / name
    path.write_text(json.dumps(scenario))
    return path


def har_entries(har_dir: Path) -> list[dict[str, Any]]:
    """The entries of the one HAR file under ``har_dir`` (asserting there is one)."""
    har_files = list(har_dir.glob("*.har"))
    assert len(har_files) == 1, f"expected exactly one .har under {har_dir}, found {har_files}"
    return json.loads(har_files[0].read_text(encoding="utf-8"))["log"]["entries"]


def named(*rows: tuple[Any, ...]) -> list[Any]:
    """Parametrize rows whose first value, a scenario name, is also the test id."""
    return [pytest.param(*row, id=row[0]) for row in rows]
