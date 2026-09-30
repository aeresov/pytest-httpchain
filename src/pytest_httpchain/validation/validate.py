"""The validator's entry points: `validate_scenario` loads one scenario file
and reports everything found, `validate_paths` does so for every file a
command line's paths stand for, directories searched."""

import os
from collections.abc import Sequence
from pathlib import Path

from pytest_httpchain.body_schema import ReferenceBounds
from pytest_httpchain.constants import SCENARIO_FILE_EXTENSIONS
from pytest_httpchain.validation.deep import check_scenario_deep
from pytest_httpchain.validation.diagnostics import Diagnostic, DiagnosticCode, ValidateResult, diag, result
from pytest_httpchain.validation.discovery import configured_suffix, find_scenario_files, pytest_rootdir
from pytest_httpchain.validation.loader import load_with_diagnostics, resolve_root_path
from pytest_httpchain.validation.semantic import check_scenario, describe_scenario


def validate_scenario(
    path: Path,
    ref_parent_traversal_depth: int = 3,
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
        diagnostics.append(
            diag(
                DiagnosticCode.WRONG_EXTENSION,
                f"File has extension '{path.suffix}' but expected '.json' or '.jsonc'. Consider renaming to use one of these extensions.",
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
    ref_parent_traversal_depth: int = 3,
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

    Every directory is searched before any file is validated, so the
    `DiscoveryError` of one that cannot be (or of a configuration file that
    cannot be read) stops the run before it has done any work.

    References are held to ``root_path``, by default to the rootdir pytest
    would determine for a run on ``paths`` (`pytest_rootdir`): one root for
    every file, as a pytest run has, where a root derived for each file
    alone could be narrower than the run's and reject a reference it
    resolves. A file that root does not hold (files of two projects in one
    run, the run's root the first one's) is held to the root pytest gives
    it alone: the run's would fail every reference it makes.
    """
    run_root = root_path if root_path is not None else pytest_rootdir(paths)

    def file_root(path: Path) -> Path:
        if root_path is not None or Path(os.path.abspath(path)).is_relative_to(run_root):
            return run_root
        return pytest_rootdir([path])

    targets: list[tuple[Path, ValidateResult | None]] = []
    seen: set[str] = set()

    def add(path: Path, found: ValidateResult | None = None) -> None:
        # Compared as pytest compares arguments: absolute, with `..` and `.`
        # dropped from the text, not resolved through symlinks.
        key = os.path.abspath(path)
        if key not in seen:
            seen.add(key)
            targets.append((path, found))

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
            found if found is not None else validate_scenario(path, ref_parent_traversal_depth=ref_parent_traversal_depth, root_path=file_root(path), deep=deep, syspaths=syspaths),
        )
        for path, found in targets
    ]
