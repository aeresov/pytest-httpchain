"""Reference path and JSON pointer helpers for reference resolution."""

import warnings
from pathlib import Path, PurePosixPath, PureWindowsPath

from pytest_httpchain.jsonref.exceptions import ReferenceResolverError
from pytest_httpchain.warnings import AmbiguousReferenceWarning


def validate_ref_path(ref_path: str, base_path: Path, root_path: Path, max_parent_traversal_depth: int) -> Path:
    """Validate and resolve a reference path.

    Args:
        ref_path: The reference path to validate
        base_path: The base path to resolve relative references from
        root_path: The root path that references should not escape
        max_parent_traversal_depth: Maximum allowed parent directory traversals

    Returns:
        The resolved absolute path

    Raises:
        ReferenceResolverError: If the path is invalid or violates security constraints
    """
    # Reject absolute ref file paths: they bypass the traversal limit (no "..")
    # and `base / "/abs"` collapses to "/abs", escaping the sandbox. Scenario
    # files are portable artifacts, so judge "absolute" under BOTH path
    # flavors — the host's Path alone would let "/etc/passwd" through on
    # Windows (rooted but not absolute there) and "C:\\x" through on POSIX.
    if PurePosixPath(ref_path).is_absolute() or PureWindowsPath(ref_path).is_absolute() or ref_path.startswith(("/", "\\")):
        raise ReferenceResolverError(f"Absolute reference paths are not allowed: {ref_path}")

    # Count parent traversals before resolution
    parent_traversals = sum(1 for part in Path(ref_path).parts if part == "..")

    if parent_traversals > max_parent_traversal_depth:
        raise ReferenceResolverError(f"Reference path '{ref_path}' exceeds maximum parent traversal depth of {max_parent_traversal_depth}")

    root_path_resolved = root_path.resolve()
    base_path_resolved = base_path.resolve()

    def is_valid_and_exists(resolved: Path) -> bool:
        """Check if path exists and is within allowed directory tree."""
        if not resolved.exists():
            return False
        try:
            resolved.relative_to(root_path_resolved)
            return True
        except ValueError:
            return False

    # Candidate base paths, in order of preference: the referencing file's
    # directory first, then the configured root_path. Resolution is a pure
    # function of the file tree + root_path (no CWD fallback), so it does
    # not depend on where the tool was launched.
    paths_to_try = [base_path]

    # Add root_path if it's different from base_path
    if root_path_resolved != base_path_resolved:
        paths_to_try.append(root_path)

    candidates = []
    for base in paths_to_try:
        resolved = (base / ref_path).resolve()
        if is_valid_and_exists(resolved) and resolved not in candidates:
            candidates.append(resolved)

    # If no existing file found, raise an error showing what paths were tried
    if not candidates:
        tried_paths = [str((base / ref_path).resolve()) for base in paths_to_try]
        paths_msg = "\n  - ".join(tried_paths)
        raise ReferenceResolverError(f"Reference path '{ref_path}' not found. Tried:\n  - {paths_msg}")

    # Both lookup bases have a matching file: the file-relative candidate
    # wins, but silently shadowing the root-relative one is surprising —
    # adding a file next to a scenario could change what a reference means.
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
    """Parse a JSON pointer into path components.

    Args:
        pointer: JSON pointer string (e.g., "/path/to/node")

    Returns:
        List of path components

    Raises:
        ReferenceResolverError: If the pointer is invalid
    """
    if not pointer:
        return []

    if not pointer.startswith("/"):
        raise ReferenceResolverError(f"Invalid JSON pointer: {pointer} (must start with '/')")

    # Split by / and handle escaped characters
    parts = []
    for part in pointer[1:].split("/"):
        # Unescape JSON pointer escape sequences
        part = part.replace("~1", "/").replace("~0", "~")
        parts.append(part)

    return parts
