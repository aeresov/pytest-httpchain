"""The validator's entry points: `validate_scenario` loads one scenario file
and reports everything found, `validate_paths` does so for every file a
command line's paths stand for, directories searched."""

import os
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from pytest_httpchain.body_schema import ReferenceBounds
from pytest_httpchain.constants import DEFAULT_REF_PARENT_TRAVERSAL_DEPTH, SCENARIO_FILE_EXTENSIONS
from pytest_httpchain.validation.deep import check_scenario_deep
from pytest_httpchain.validation.diagnostics import Diagnostic, DiagnosticCode, ValidateResult, diag, result
from pytest_httpchain.validation.discovery import absolute, configured_depth, configured_suffix, find_scenario_files, pytest_rootdir
from pytest_httpchain.validation.loader import load_with_diagnostics, resolve_root_path
from pytest_httpchain.validation.semantic import check_scenario, describe_scenario


class _Bounds(NamedTuple):
    """The root and the parent traversal depth a file's references are held
    to."""

    root: Path
    depth: int


def validate_scenario(
    path: Path,
    ref_parent_traversal_depth: int = DEFAULT_REF_PARENT_TRAVERSAL_DEPTH,
    root_path: Path | None = None,
    deep: bool = False,
    syspaths: list[Path] | None = None,
) -> ValidateResult:
    """Validate a scenario file: existence, JSON, ``$ref``, schema, then the
    semantic checks — and with ``deep``, the opt-in import/file checks.

    References are held to ``root_path``, by default pytest's rootdir for a
    run on the file (`resolve_root_path`)."""
    diagnostics: list[Diagnostic] = []

    if not path.exists():
        return result([diag(DiagnosticCode.FILE_NOT_FOUND, f"File not found: {path}")])

    if not path.is_file():
        return result([diag(DiagnosticCode.NOT_A_FILE, f"Path is not a file: {path}")])

    if path.suffix.lower() not in SCENARIO_FILE_EXTENSIONS:
        expected = " or ".join(f"'{extension}'" for extension in SCENARIO_FILE_EXTENSIONS)
        diagnostics.append(
            diag(
                DiagnosticCode.WRONG_EXTENSION,
                f"File has extension '{path.suffix}' but expected {expected}. Consider renaming to use one of these extensions.",
                location=str(path),
            )
        )

    if root_path is None:
        root_path = resolve_root_path(path)

    # Load diagnostics are collected rather than returned early, so ambiguity
    # warnings earned by earlier references are still reported.
    loaded, load_diagnostics = load_with_diagnostics(path, root_path=root_path, ref_parent_traversal_depth=ref_parent_traversal_depth)
    diagnostics.extend(load_diagnostics)
    if loaded is None:
        return result(diagnostics)

    scenario, test_data = loaded
    diagnostics.extend(check_scenario(scenario, test_data))

    if deep:
        # The bounds the load held the scenario's references to, for its body schemas'.
        ref_bounds = ReferenceBounds(root_path, ref_parent_traversal_depth)
        diagnostics.extend(check_scenario_deep(scenario, syspaths=syspaths, scenario_dir=path.parent, ref_bounds=ref_bounds))

    return result(diagnostics, describe_scenario(scenario, test_data))


def validate_paths(
    paths: Sequence[Path],
    suffix: str | None = None,
    ref_parent_traversal_depth: int | None = None,
    root_path: Path | None = None,
    deep: bool = False,
    syspaths: list[Path] | None = None,
) -> list[tuple[Path, ValidateResult]]:
    """Validate what each of ``paths`` stands for, sorted by path.

    A directory stands for the scenario files under it (`find_scenario_files`),
    named by ``suffix``, or when that is None by the suffix pytest's
    configuration sets for these paths (`configured_suffix`, read at the first
    directory); anything else for itself, a file validated whatever its name
    (`validate_scenario`). A file reached twice, as ``tests tests/api`` reach
    ``tests/api``'s, is validated once, under the path it was first reached
    by. A directory holding no scenario file is a result of its own, an
    ``HTTPCHAIN039`` error: a mistyped path, or a suffix that names no file,
    must not pass as a clean run.

    The results are sorted by their paths, compared name by name
    (`Path.parts`), whatever order ``paths`` come in: a report that does not
    depend on how a shell or ``find`` listed the arguments. Within a directory
    that is the order pytest collects in (`find_scenario_files`).

    Every directory is searched, and every file's depth read, before any
    file is validated, so the `DiscoveryError` of a directory that cannot be
    searched (or of a configuration file that cannot be read, or a depth it
    sets that pytest would refuse) stops the run before it has done any
    work.

    References are held to ``root_path``, by default to the rootdir pytest
    would determine for a run on ``paths`` (`pytest_rootdir`): one root for
    every file, as a pytest run has, where a root derived for each file
    alone could be narrower than the run's and reject a reference it
    resolves. They climb at most ``ref_parent_traversal_depth`` parent
    directories, when that is None the depth pytest's configuration sets for
    these paths (`configured_depth`). A file the run's root does not hold
    (files of two projects in one run, the run's root the first one's),
    unless ``root_path`` is given, is held to the root, and the depth, pytest
    gives it alone, its own project's: the run's root would fail every
    reference it makes, and the run's depth is the other project's.
    """
    run_root = root_path if root_path is not None else pytest_rootdir(paths)
    run_depth = ref_parent_traversal_depth if ref_parent_traversal_depth is not None else configured_depth(paths)

    def bounds(path: Path) -> _Bounds:
        """The root and the depth ``path``'s references are held to. A path
        that is no file is reported as such before either is used: it takes
        the run's, and no configuration is read for it."""
        if root_path is not None or not os.path.isfile(path) or absolute(path).is_relative_to(run_root):
            return _Bounds(run_root, run_depth)
        depth = ref_parent_traversal_depth if ref_parent_traversal_depth is not None else configured_depth([path])
        return _Bounds(pytest_rootdir([path]), depth)

    # Each path's result, a directory's found in the search, or the root and
    # the depth the file is to be validated with.
    targets: list[tuple[Path, ValidateResult | _Bounds]] = []
    seen: set[str] = set()

    def add(path: Path, found: ValidateResult | None = None) -> None:
        # Compared as pytest compares arguments (`absolute`), and as text: a
        # Path compares case-insensitively on Windows.
        key = str(absolute(path))
        if key not in seen:
            seen.add(key)
            # A file's bounds are read here, before the first file is
            # validated: a depth pytest would refuse in its own project's
            # configuration stops the run before it has done any work too.
            targets.append((path, found if found is not None else bounds(path)))

    for path in paths:
        if not path.is_dir():
            add(path)
            continue
        if suffix is None:
            suffix = configured_suffix(paths)
        files = list(find_scenario_files(path, suffix))
        if not files:
            names = " or ".join(f"test_<name>.{suffix}{extension}" for extension in SCENARIO_FILE_EXTENSIONS)
            add(path, result([diag(DiagnosticCode.NO_SCENARIO_FILES, f"No scenario files named {names} in directory: {path}")]))
        for file in files:
            add(file)

    targets.sort(key=lambda target: target[0].parts)
    return [
        (
            path,
            target if isinstance(target, ValidateResult) else validate_scenario(path, ref_parent_traversal_depth=target.depth, root_path=target.root, deep=deep, syspaths=syspaths),
        )
        for path, target in targets
    ]
