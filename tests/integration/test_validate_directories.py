"""`validate` over a directory checks the files `pytest` over it collects.

The search (validation/discovery.py) follows what pytest documents rather than
calling into it, so one tree pins the two together: the suffix pytest's
configuration sets, the names collected, the directories skipped by default,
the entries passed over, and the order."""

import json
import os

from pytest_httpchain.validation import validate_paths

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
