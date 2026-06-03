"""Unit tests for the pytest-httpchain CLI."""

import json
from pathlib import Path

from typer.testing import CliRunner

from pytest_httpchain.cli import app

runner = CliRunner()


def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data))
    return path


def _stage(name: str, url: str) -> dict:
    return {"name": name, "request": {"url": url}, "response": [{"verify": {"status": 200}}]}


def test_validate_ok_exit_zero(tmp_path):
    f = _write(tmp_path / "ok.json", {"stages": [_stage("s", "https://x.test/a")]})
    result = runner.invoke(app, ["validate", str(f)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_validate_invalid_exit_one(tmp_path):
    f = _write(
        tmp_path / "bad.json",
        {"stages": [_stage("dup", "https://x.test/a"), _stage("dup", "https://x.test/b")]},
    )
    result = runner.invoke(app, ["validate", str(f)])
    assert result.exit_code == 1, result.output
    assert "INVALID" in result.output
    assert "Duplicate stage names" in result.output


def test_validate_reports_warnings(tmp_path):
    f = _write(
        tmp_path / "warn.json",
        {"stages": [{"name": "s", "request": {"url": "https://x.test/{{ ghost }}"}, "response": [{"verify": {"status": 200}}]}]},
    )
    result = runner.invoke(app, ["validate", str(f)])
    assert result.exit_code == 0, result.output
    assert "ghost" in result.output


def test_validate_multiple_files_one_bad_exits_one(tmp_path):
    good = _write(tmp_path / "good.json", {"stages": [_stage("s", "https://x.test/a")]})
    bad = _write(
        tmp_path / "bad.json",
        {"stages": [_stage("dup", "https://x.test/a"), _stage("dup", "https://x.test/b")]},
    )
    result = runner.invoke(app, ["validate", str(good), str(bad)])
    assert result.exit_code == 1, result.output
