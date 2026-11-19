"""`validate` over a directory checks the files `pytest` over it collects.

The search (validation/discovery.py) follows what pytest documents rather than
calling into it, so one tree pins the two together: the suffix pytest's
configuration sets, the names collected, the directories skipped by default,
the entries passed over, and the order. The rootdir, the reference depth the
files are held to, and the configuration files pytest stops on before it
collects are pinned the same way."""

import json
import os
import re
from pathlib import Path

import pytest

from pytest_httpchain.validation import DiscoveryError, configured_depth, validate_paths
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


@pytest.mark.parametrize(
    ("filename", "content", "refused"),
    [
        # Text, a TOML integer in a table that keeps TOML's types, and one the
        # string-based table turns into text.
        pytest.param("pytest.ini", "[pytest]\nhttpchain_ref_parent_traversal_depth = 4\n", False, id="ini-text"),
        pytest.param("pytest.toml", "[pytest]\nhttpchain_ref_parent_traversal_depth = 4\n", False, id="toml-integer"),
        pytest.param("pyproject.toml", "[tool.pytest.ini_options]\nhttpchain_ref_parent_traversal_depth = 4\n", False, id="ini_options-integer"),
        # What pytest, or the plugin, refuses.
        pytest.param("pyproject.toml", '[tool.pytest]\nhttpchain_ref_parent_traversal_depth = "4"\n', True, id="toml-text"),
        pytest.param("pytest.toml", "[pytest]\nhttpchain_ref_parent_traversal_depth = true\n", True, id="toml-bool"),
        pytest.param("pyproject.toml", "[tool.pytest.ini_options]\nhttpchain_ref_parent_traversal_depth = 4.0\n", True, id="ini_options-float"),
        pytest.param("pytest.ini", "[pytest]\nhttpchain_ref_parent_traversal_depth = -1\n", True, id="negative"),
    ],
)
def test_validate_reads_the_depth_pytest_reads(pytester, filename, content, refused):
    """A scenario including a file four levels up, one more than the default
    allows: collected and validated alike under the depth each form sets, or
    refused by both, by `validate` before it checks anything."""
    (pytester.path / filename).write_text(content)
    (pytester.path / "shared.json").write_text(json.dumps(SCENARIO["stages"][0]["request"]))
    directory = pytester.path / "a" / "b" / "c" / "d"
    directory.mkdir(parents=True)
    scenario = {"stages": [{**SCENARIO["stages"][0], "request": {"$include": "../../../../shared.json"}}]}
    (directory / "test_x.http.json").write_text(json.dumps(scenario))

    result = pytester.runpytest("--collect-only", "-q")

    if refused:
        # pytest 9.0 lets the TypeError of a TOML value of another type
        # through as an internal error; later ones make it a usage error.
        assert result.ret in (pytest.ExitCode.USAGE_ERROR, pytest.ExitCode.INTERNAL_ERROR)
        with pytest.raises(DiscoveryError, match=rf"^{re.escape(str(pytester.path / filename))}: httpchain_ref_parent_traversal_depth must be "):
            validate_paths([pytester.path])
    else:
        result.assert_outcomes()
        result.stdout.fnmatch_lines(["*test_x.http.json*"])
        assert [validated.diagnostics for _, validated in validate_paths([pytester.path])] == [[]]


def _collect(pytester) -> tuple[list[str], str | None]:
    """The files a collection of pytester's directory collects, and what it
    stops on before collecting any, in pytest's words, if it does."""
    try:
        result = pytester.runpytest("--collect-only", "-q")
    except pytest.fail.Exception as e:
        # A setup.cfg's [pytest] section: pytest stops on it with
        # pytest.fail, which a run in this process lets through.
        return [], str(e)
    if result.ret == pytest.ExitCode.USAGE_ERROR:
        [line] = [line for line in result.errlines if line.startswith("ERROR: ")]
        return [], line.removeprefix("ERROR: ")
    result.assert_outcomes()
    return list(dict.fromkeys(line.split("::")[0] for line in result.outlines if "::" in line)), None


CONFIGURED = "[pytest]\nhttpchain_suffix = api\n"


@pytest.mark.parametrize(
    ("layout", "named"),
    [
        # Beside the configuration file, after it in pytest's order, files
        # pytest stops on, on every release.
        pytest.param({"pytest.ini": CONFIGURED, "pyproject.toml": "[tool.pytest\n"}, "pyproject.toml", id="toml-syntax"),
        pytest.param({"pytest.ini": CONFIGURED, "tox.ini": "  a continuation line first\n"}, "tox.ini", id="ini-syntax"),
        pytest.param({"pytest.ini": CONFIGURED, "pyproject.toml": '[tool.pytest]\nx = 1\n[tool.pytest.ini_options]\ny = "2"\n'}, "pyproject.toml", id="both-tables"),
        pytest.param({"pytest.ini": CONFIGURED, "setup.cfg": "[pytest]\nx = 1\n"}, "setup.cfg", id="setup.cfg-pytest-section"),
        # And files it reads there and goes on from: holding no pytest
        # configuration, or some, which it ignores.
        pytest.param(
            {"pytest.ini": CONFIGURED, "pyproject.toml": '[tool.pytest]\nhttpchain_suffix = "other"\n', "tox.ini": "[tox]\n", "setup.cfg": "[metadata]\nname = a\n"},
            None,
            id="readable",
        ),
        # Options outside any table of a pytest.toml, or of a .pytest.toml
        # beside one, which pytest stops on from 9.2.0 on and, before, takes
        # as setting nothing.
        pytest.param({"pytest.toml": 'httpchain_suffix = "api"\n'}, "pytest.toml", id="pytest.toml-top-level"),
        pytest.param({"pytest.toml": '[pytest]\nhttpchain_suffix = "api"\n', ".pytest.toml": "a = 1\n"}, ".pytest.toml", id=".pytest.toml-top-level-beside"),
        pytest.param({"pytest.toml": "[tool.x]\na = 1\n"}, None, id="pytest.toml-tables-only"),
    ],
)
def test_validate_stops_on_the_configuration_files_pytest_stops_on(pytester, layout, named):
    """On the pytest installed: `validate` over a directory stops where a
    collection of it stops, on the file named, else checks the files it
    collects, named by the suffix it reads. The file pytest names (but for
    a setup.cfg's [pytest] section, which it names none for) is the one
    `validate` names, in pytest's own words for a pytest.toml's options."""
    for name, content in layout.items():
        (pytester.path / name).write_text(content)
    for name in ("test_x.http.json", "test_y.api.json"):
        (pytester.path / name).write_text(json.dumps(SCENARIO))

    collected, stopped = _collect(pytester)
    try:
        validated = [path.relative_to(pytester.path).as_posix() for path, _ in validate_paths([pytester.path])]
    except DiscoveryError as e:
        validated, refused = [], str(e)
    else:
        refused = None

    assert (refused is None) == (stopped is None), (refused, stopped)
    assert collected == validated
    if stopped is None or refused is None:  # both, by the assertion above
        assert len(collected) == 1
    else:
        assert named is not None
        file = str(pytester.path / named)
        assert file in refused
        assert file in stopped or refused == f"{file}: {stopped}"
        if "top-level options" in stopped:
            assert refused == stopped
        assert configured_depth([pytester.path]) == 3
