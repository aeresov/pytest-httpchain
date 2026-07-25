"""Reference path and JSON pointer helpers for reference resolution."""

import warnings
from pathlib import Path, PurePosixPath, PureWindowsPath

from pytest_httpchain.jsonref.exceptions import ReferenceResolverError
from pytest_httpchain.warnings import AmbiguousReferenceWarning


def validate_ref_path(ref_path: str, base_path: Path, root_path: Path, max_parent_traversal_depth: int) -> Path:
    """Resolve a reference path against its lookup bases, or raise.

    Tried in order: the referencing file's directory, then ``root_path``. The
    result must exist and stay inside ``root_path``.
    """
    # Absolute paths bypass the traversal limit and escape the sandbox, and
    # scenario files are portable — so judge "absolute" under both flavors: the
    # host's Path alone lets "/etc/passwd" through on Windows and "C:\\x" on POSIX.
    if PurePosixPath(ref_path).is_absolute() or PureWindowsPath(ref_path).is_absolute() or ref_path.startswith(("/", "\\")):
        raise ReferenceResolverError(f"Absolute reference paths are not allowed: {ref_path}")

    parent_traversals = sum(1 for part in Path(ref_path).parts if part == "..")

    if parent_traversals > max_parent_traversal_depth:
        raise ReferenceResolverError(f"Reference path '{ref_path}' exceeds maximum parent traversal depth of {max_parent_traversal_depth}")

    root_path_resolved = root_path.resolve()
    base_path_resolved = base_path.resolve()

    def is_valid_and_exists(resolved: Path) -> bool:
        if not resolved.exists():
            return False
        try:
            resolved.relative_to(root_path_resolved)
            return True
        except ValueError:
            return False

    # No CWD fallback: resolution must not depend on where the tool was launched.
    paths_to_try = [base_path]
    if root_path_resolved != base_path_resolved:
        paths_to_try.append(root_path)

    candidates = []
    for base in paths_to_try:
        resolved = (base / ref_path).resolve()
        if is_valid_and_exists(resolved) and resolved not in candidates:
            candidates.append(resolved)

    if not candidates:
        tried_paths = [str((base / ref_path).resolve()) for base in paths_to_try]
        paths_msg = "\n  - ".join(tried_paths)
        raise ReferenceResolverError(f"Reference path '{ref_path}' not found. Tried:\n  - {paths_msg}")

    # File-relative wins, but shadowing the other silently is surprising.
    if len(candidates) > 1:
        warnings.warn(
            AmbiguousReferenceWarning(
                f"Reference '{ref_path}' matches an existing file under both the referencing "
                f"file's directory and the root path; using {candidates[0]} (file-relative wins), "
                f"ignoring {candidates[1]}"
            ),
            stacklevel=2,
        )

    return candidates[0]


def parse_json_pointer(pointer: str) -> list[str]:
    """Split a JSON pointer into its unescaped components."""
    if not pointer:
        return []

    if not pointer.startswith("/"):
        raise ReferenceResolverError(f"Invalid JSON pointer: {pointer} (must start with '/')")

    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
