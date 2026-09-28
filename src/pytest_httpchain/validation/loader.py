"""The load + $ref-resolve + model-validate path shared by the CLI and pytest collection."""

import configparser
import json
import tomllib
import warnings
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pytest_httpchain.jsonref import InvalidJSONError, ReferenceResolverError, load_json
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


def _verify_subpath(path: tuple[str | int, ...]) -> tuple[str | int, ...] | None:
    """The part of a raw-JSON position below a step's ``verify``, or None when
    the position is not inside one.

    The grammar is ``stages[K].response[K].verify``, where stages and response
    steps accept both the list form (``K`` is an index) and the name-keyed
    mapping form, and a response mapping value may itself be a list.
    """
    match path:
        case ("stages", _, "response", _, "verify", *rest):
            return tuple(rest)
        case ("stages", _, "response", _, int(), "verify", *rest):
            return tuple(rest)
        case _:
            return None


def is_inline_schema_position(path: tuple[str | int, ...]) -> bool:
    """True for raw-JSON positions holding an inline verify schema, whose
    ``$ref``/``$defs`` address the schema validator rather than the scenario's
    reference resolver (passed to the loader as its ``opaque`` predicate)."""
    return _verify_subpath(path) == ("body", "schema")


def is_alternatives_position(path: tuple[str | int, ...]) -> bool:
    """True for raw-JSON positions whose list holds alternatives, any one of
    which passes: ``verify.status``, and a stage's ``retry.on``, any one of
    whose kinds makes another attempt (one half of the loader's ``atomic``
    predicate, `merges_whole`).

    Everywhere else a longer list checks more, so a sibling list merged onto a
    fragment's is concatenated. Here concatenation would widen the check: a
    sibling ``[404]`` on a fragment's ``["2xx"]`` would pass a 200, and a
    sibling ``["request"]`` written to narrow a shared ``["verify"]`` would
    resend a request that timed out. The value merges as a whole instead, as a
    scalar does: equal keeps, different is a merge conflict.
    """
    match path:
        case ("stages", _, "retry", "on"):
            return True
    return _verify_subpath(path) == ("status",)


def is_jmespath_expectations_position(path: tuple[str | int, ...]) -> bool:
    """True for raw-JSON positions holding a ``verify.jmespath`` mapping, whose
    keys are JMESPath expressions rather than names sent on the wire."""
    return _verify_subpath(path) == ("jmespath",)


def is_expected_value_position(path: tuple[str | int, ...]) -> bool:
    """True for raw-JSON positions holding what one ``verify.jmespath``
    expression must be (a value it must equal, or a matcher), and for each
    operand of such a matcher.

    An expected value is one value, not a list of checks: a sibling's
    ``["b"]`` concatenated onto a fragment's ``["a"]`` would assert
    ``["a", "b"]``, which neither side wrote, and two objects under ``eq``
    would blend into a third. So the expectation merges as a whole, as a
    scalar does: equal keeps, different is a merge conflict. Different
    expressions still merge key by key.

    A matcher's operand is one value for the same reason. It only merges with
    another when a reference is written at the expectation itself (the merge
    root is exempt from ``atomic``, so ``{"$merge": ..., "lt": 100}`` composes
    a matcher key by key): an ``eq`` or ``contains`` both sides give must then
    agree whole, as a ``gt`` does. One level below the expectation is always
    an operand: an array expectation is kept whole before its items are
    reached, and a reference with siblings is an object.
    """
    subpath = _verify_subpath(path)
    return subpath is not None and len(subpath) in (2, 3) and subpath[0] == "jmespath"


def merges_whole(path: tuple[str | int, ...]) -> bool:
    """The loader's ``atomic`` predicate: `is_alternatives_position` or
    `is_expected_value_position`."""
    return is_alternatives_position(path) or is_expected_value_position(path)


def load_scenario_json(path: Path, *, root_path: Path | None = None, ref_parent_traversal_depth: int = 3) -> dict[str, Any]:
    """Load a scenario file and ``$ref``-resolve it, unvalidated: the document
    collection, ``validate`` and ``resolve`` all see.

    Raises ``ReferenceResolverError`` / ``json.JSONDecodeError`` / ``OSError``.
    """
    if root_path is None:
        root_path = resolve_root_path(path)
    return load_json(
        path,
        max_parent_traversal_depth=ref_parent_traversal_depth,
        root_path=root_path,
        opaque=is_inline_schema_position,
        atomic=merges_whole,
    )


def load_scenario(path: Path, *, root_path: Path | None = None, ref_parent_traversal_depth: int = 3) -> tuple[Scenario, dict[str, Any]]:
    """Load, ``$ref``-resolve and validate a scenario file -> ``(scenario, raw_data)``.

    Raises ``ReferenceResolverError`` / ``json.JSONDecodeError`` /
    ``pydantic.ValidationError``; callers map these to user-facing errors.
    """
    test_data = load_scenario_json(path, root_path=root_path, ref_parent_traversal_depth=ref_parent_traversal_depth)
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
            # Content the reader rejected (not UTF-8 — RFC 8259 requires it — a
            # duplicate key, ...) and a plain syntax error the resolver wrapped
            # are JSON content problems — no reference is involved in either.
            # Deep nesting is not: the text is valid, but deeper than the
            # parser or the resolver can go.
            if isinstance(e, InvalidJSONError):
                diagnostics.append(diag(DiagnosticCode.INVALID_JSON, f"Invalid JSON: {e}"))
            elif isinstance(e.__cause__, json.JSONDecodeError):
                diagnostics.append(diag(DiagnosticCode.INVALID_JSON, f"Invalid JSON syntax: {e.__cause__}"))
            elif isinstance(e.__cause__, RecursionError):
                diagnostics.append(diag(DiagnosticCode.PARSE_ERROR, f"Failed to parse JSON file: nested too deeply ({e.__cause__})"))
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
