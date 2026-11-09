"""Builders for scenarios written inline by an integration test, table rows,
and the HAR files a run exports.

A plain helpers module (not conftest.py, which pytest treats as a plugin file,
not an import target)::

    from tests.integration.helpers import HAR_ARGS, har_entries, named, stage
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


# The reference adapter of docs/usage/connections.md, an aux file for
# `run_scenario`, and a connection to the examples' `line_json_server` through it.
LINE_JSON_ADAPTER = "connections/line_json.py"
LINE_JSON = {"adapter": "line_json:LineJsonAdapter", "params": {"host": "127.0.0.1", "port": "{{ env('HTTPCHAIN_EXAMPLE_LINE_JSON_PORT') }}"}}


def connection_stage(name: str, connection: str = "device", *, response: list[Any] | None = None, **step: Any) -> dict[str, Any]:
    """One connection stage: ``step`` is its connection step's actions
    (``open``, ``send``, ``expect``, ...) on ``connection``. Stage keys other
    than ``response`` go in the step's ``stage`` entry, when there are any:
    ``connection_stage("s", send={...}, stage={"retry": {...}})``."""
    fields = step.pop("stage", {})
    built = {"name": name, "connection": {"name": connection, **step}, **fields}
    if response is not None:
        built["response"] = response
    return built


def line_json_scenario(*stages: dict[str, Any], connections: tuple[str, ...] = ("device",), **fields: Any) -> dict[str, Any]:
    """A scenario of ``stages`` whose ``connections`` each reach the
    examples' class-scoped ``line_json_server`` through the reference adapter
    (`LINE_JSON_ADAPTER`, which the run needs as an aux file)."""
    return {"marks": ["usefixtures('line_json_server')"], "connections": dict.fromkeys(connections, LINE_JSON), "stages": list(stages), **fields}


def write_scenario(directory: Path, scenario: dict[str, Any], name: str = "test_inline.http.json") -> Path:
    path = directory / name
    path.write_text(json.dumps(scenario))
    return path


# The args that make a run write HAR files for `har_entries` to read, under
# "har_out" relative to the pytester dir, which is the CWD of both in-process
# and subprocess runs.
HAR_ARGS = ("-s", "--httpchain-output-dir", "har_out")


def har_entries(har_dir: Path) -> list[dict[str, Any]]:
    """The entries of the one HAR file under ``har_dir`` (asserting there is one)."""
    har_files = list(har_dir.glob("*.har"))
    assert len(har_files) == 1, f"expected exactly one .har under {har_dir}, found {har_files}"
    return json.loads(har_files[0].read_text(encoding="utf-8"))["log"]["entries"]


def named(*rows: tuple[Any, ...]) -> list[Any]:
    """Parametrize rows whose first value, a scenario name, is also the test id."""
    return [pytest.param(*row, id=row[0]) for row in rows]
