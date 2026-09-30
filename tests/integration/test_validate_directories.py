"""`validate` over a directory checks the files `pytest` over it collects.

The search (validation/discovery.py) follows what pytest documents rather than
calling into it, so one tree pins the two together: the suffix pytest's
configuration sets, the names collected, the directories skipped by default,
the entries passed over, and the order."""

import json
import os
from pathlib import Path

import pytest

from pytest_httpchain.validation import validate_paths
from pytest_httpchain.validation.discovery import pytest_rootdir

SCENARIO = {"stages": [{"name": "s", "request": {"url": "https://x.test/a"}, "response": [{"verify": {"status": 200}}]}]}


def test_validate_checks_the_files_pytest_collects_in_its_order(pytester):
    pytester.makeini("[pytest]\nhttpchain_suffix = api\n")
    for name in (
        "test_top.api.json",
        "suite/test_nested.api.jsonc",
        "suite/deeper/test_deep.api.json",
        # Another suffix, and no `test_` prefix.
        "test_other.http.json",
        "login.api.json",
        # Directories pytest skips by default.
        "suite/build/test_built.api.json",
        ".hidden/test_hidden.api.json",
        "node_modules/pkg/test_module.api.json",
        "pkg.egg/test_egg.api.json",
        "__pycache__/test_cached.api.json",
        "env/test_venv.api.json",
        "env/pyvenv.cfg",
        "condaenv/lib/test_conda.api.json",
        "condaenv/conda-meta/history",
    ):
        path = pytester.path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(SCENARIO))
    if os.name != "nt":
        # A link to itself, named like a scenario: looking at it fails, and
        # the rest of the tree is still searched.
        (pytester.path / "test_loop.api.json").symlink_to("test_loop.api.json")

    result = pytester.runpytest("--collect-only", "-q")
    collected = list(dict.fromkeys(line.split("::")[0] for line in result.outlines if "::" in line))
    validated = [path.relative_to(pytester.path).as_posix() for path, _ in validate_paths([pytester.path])]

    assert collected == validated == ["suite/deeper/test_deep.api.json", "suite/test_nested.api.jsonc", "test_top.api.json"]


def _rootdir(result) -> Path:
    [line] = [line for line in result.outlines if line.startswith("rootdir: ")]
    return Path(line.removeprefix("rootdir: ").split(",")[0])


@pytest.mark.parametrize(
    ("layout", "args", "reference"),
    [
        # A pytest.toml at the top, below it a package whose own
        # pyproject.toml configures no pytest: the rootdir is the top.
        pytest.param({"pytest.toml": "[pytest]\n", "packages/api/pyproject.toml": '[project]\nname = "api"\n'}, ["packages/api/tests"], "../../../shared.json", id="pytest.toml"),
        # No configuration: the rootdir of a run on two directories is their
        # common ancestor, which a reference beside them stays inside.
        pytest.param({}, ["a", "b"], "../shared.json", id="two-paths"),
    ],
)
def test_validate_holds_references_to_the_rootdir_pytest_reports(pytester, layout, args, reference):
    """`validate` on the paths a run is given resolves the references that
    run's collection resolves, since both hold them to the rootdir pytest
    determines for those paths."""
    for name, content in layout.items():
        (pytester.path / name).parent.mkdir(parents=True, exist_ok=True)
        (pytester.path / name).write_text(content)
    (pytester.path / "shared.json").write_text(json.dumps(SCENARIO["stages"][0]["request"]))
    for arg in args:
        scenario = {"stages": [{**SCENARIO["stages"][0], "request": {"$include": reference}}]}
        (pytester.path / arg).mkdir(parents=True, exist_ok=True)
        (pytester.path / arg / "test_x.http.json").write_text(json.dumps(scenario))

    result = pytester.runpytest("--collect-only", *args)

    result.assert_outcomes()
    result.stdout.fnmatch_lines([f"collected {len(args)} item*"])
    assert _rootdir(result) == pytest_rootdir([Path(arg) for arg in args]) == pytester.path
    assert [validated.diagnostics for _, validated in validate_paths([Path(arg) for arg in args])] == [[]] * len(args)
