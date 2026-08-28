"""The load + $ref-resolve + model-validate path shared by the CLI and pytest collection."""

import configparser
import json
import tomllib
import warnings
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pytest_httpchain.jsonref import DuplicateKeyError, ReferenceResolverError, load_json
from pytest_httpchain.models import Scenario
from pytest_httpchain.validation.diagnostics import Diagnostic, DiagnosticCode, diag
from pytest_httpchain.warnings import AmbiguousReferenceWarning

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


def load_with_diagnostics(
    path: Path,
    *,
    root_path: Path | None = None,
    ref_parent_traversal_depth: int = 3,
) -> tuple[tuple[Scenario, dict[str, Any]] | None, list[Diagnostic]]:
    """`load_scenario`, with every failure mapped to a coded `Diagnostic`.

    The single owner of the load-failure taxonomy, so ``validate`` and pytest
    collection cannot report the same broken file differently — they once did:
    the CLI said ``[HTTPCHAIN014] Invalid JSON syntax``, collection said an
    uncoded "Cannot load JSON file".

    Returns ``(None, diagnostics)`` when the file could not be loaded. Resolver
    warnings are recorded rather than raised: under ``filterwarnings = error``
    an escaping warning would surface as a misleading parse failure. Ambiguity
    warnings become HTTPCHAIN026; anything else is re-emitted at its original
    site, after the load, so a caller's own filters still apply.
    """
    diagnostics: list[Diagnostic] = []
    loaded: tuple[Scenario, dict[str, Any]] | None = None

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        try:
            loaded = load_scenario(path, root_path=root_path, ref_parent_traversal_depth=ref_parent_traversal_depth)
        except ReferenceResolverError as e:
            # A duplicate key, and a plain syntax error the resolver wrapped, are
            # JSON content problems — no reference is involved in either.
            if isinstance(e, DuplicateKeyError):
                diagnostics.append(diag(DiagnosticCode.INVALID_JSON, f"Invalid JSON: {e}"))
            elif isinstance(e.__cause__, json.JSONDecodeError):
                diagnostics.append(diag(DiagnosticCode.INVALID_JSON, f"Invalid JSON syntax: {e.__cause__}"))
            else:
                diagnostics.append(diag(DiagnosticCode.REF_ERROR, f"JSON reference resolution error: {e}"))
        except json.JSONDecodeError as e:
            diagnostics.append(diag(DiagnosticCode.INVALID_JSON, f"Invalid JSON syntax: {e}"))
        except ValidationError as e:
            for err in e.errors():
                loc = " -> ".join(str(x) for x in err["loc"])
                diagnostics.append(diag(DiagnosticCode.SCHEMA, f"Schema validation failed: {loc}: {err['msg']}", location=loc))
        except Exception as e:
            diagnostics.append(diag(DiagnosticCode.PARSE_ERROR, f"Failed to parse JSON file: {e}"))

    for caught in caught_warnings:
        if isinstance(caught.message, AmbiguousReferenceWarning):
            diagnostics.append(diag(DiagnosticCode.AMBIGUOUS_REF, str(caught.message)))
        else:
            warnings.warn_explicit(caught.message, caught.category, caught.filename, caught.lineno)

    return loaded, diagnostics
