"""The load + $ref-resolve + model-validate path shared by the CLI and pytest collection."""

import configparser
import tomllib
from pathlib import Path
from typing import Any

from pytest_httpchain.jsonref import load_json
from pytest_httpchain.models import Scenario

_ROOT_MARKERS = ("pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg", "setup.py", ".git")


def _holds_pytest_config(directory: Path) -> bool:
    """True when the directory holds a file pytest would accept as its inifile.

    Mirrors pytest's own rule: ``pytest.ini`` always counts, while the shared
    files count only when they actually carry a pytest section. A bare
    ``pyproject.toml`` — a sub-package in a monorepo — is not a pytest rootdir.
    """
    if (directory / "pytest.ini").is_file():
        return True

    pyproject = directory / "pyproject.toml"
    if pyproject.is_file():
        try:
            if "pytest" in tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}):
                return True
        except (OSError, ValueError):
            pass

    for filename, section in (("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        candidate = directory / filename
        if not candidate.is_file():
            continue
        parser = configparser.ConfigParser()
        try:
            parser.read(candidate, encoding="utf-8")
        except (OSError, configparser.Error, UnicodeDecodeError):
            continue
        if parser.has_section(section):
            return True

    return False


def resolve_root_path(path: Path) -> Path:
    """Directory that constrains ``$ref`` resolution when no explicit root is given.

    Approximates pytest's ``rootdir`` (which collection passes explicitly): a
    real pytest config wins outright, even when a bare project marker sits
    nearer. Without that precedence a sub-package's plain ``pyproject.toml``
    shrinks the CLI's root below pytest's, and ``validate`` rejects ``$ref``
    targets that collection resolves fine — a red CI on a working scenario.
    Falls back to the nearest bare marker, then the nearest ``tests/`` ancestor,
    then the file's own parent.
    """
    ancestors = path.resolve().parents
    for ancestor in ancestors:
        if _holds_pytest_config(ancestor):
            return ancestor
    for ancestor in ancestors:
        if any((ancestor / marker).exists() for marker in _ROOT_MARKERS):
            return ancestor
    for ancestor in ancestors:
        if ancestor.name == "tests":
            return ancestor
    return path.parent


def is_inline_schema_position(path: tuple[str | int, ...]) -> bool:
    """True for raw-JSON positions holding an inline verify schema, whose
    ``$ref``/``$defs`` address the schema validator rather than the scenario's
    reference resolver (passed to the loader as its ``opaque`` predicate).

    The grammar is ``stages[K].response[K].verify.body.schema``, where stages and
    response steps accept both the list form (``K`` is an index) and the
    name-keyed mapping form, and a response mapping value may itself be a list.
    """
    if path[-3:] != ("verify", "body", "schema"):
        return False
    head = path[:-3]
    if len(head) == 4:
        return head[0] == "stages" and head[2] == "response"
    if len(head) == 5:
        return head[0] == "stages" and head[2] == "response" and isinstance(head[4], int)
    return False


def load_scenario(path: Path, *, root_path: Path | None = None, ref_parent_traversal_depth: int = 3) -> tuple[Scenario, dict[str, Any]]:
    """Load, ``$ref``-resolve and validate a scenario file -> ``(scenario, raw_data)``.

    Raises ``ReferenceResolverError`` / ``json.JSONDecodeError`` /
    ``pydantic.ValidationError``; callers map these to user-facing errors.
    """
    if root_path is None:
        root_path = resolve_root_path(path)
    test_data = load_json(path, max_parent_traversal_depth=ref_parent_traversal_depth, root_path=root_path, opaque=is_inline_schema_position)
    return Scenario.model_validate(test_data), test_data
