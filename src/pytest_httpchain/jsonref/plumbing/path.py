"""Reference path and JSON pointer helpers for reference resolution."""

import re
import warnings
from pathlib import Path, PurePosixPath, PureWindowsPath

from pytest_httpchain.jsonref.exceptions import ReferenceResolverError
from pytest_httpchain.warnings import AmbiguousReferenceWarning


def validate_ref_path(ref_path: str, base_path: Path, root_path: Path, max_parent_traversal_depth: int) -> Path:
    """Resolve a reference path against its lookup bases, or raise.

    Tried in order: the referencing file's directory, then ``root_path``. The
    result must exist and stay inside ``root_path``.
    """
    # No filesystem accepts a NUL byte: resolve() below would raise a bare
    # ValueError, escaping every caller as a traceback.
    if "\0" in ref_path:
        raise ReferenceResolverError(f"Reference path contains a NUL character: {ref_path!r}")

    # Absolute paths bypass the traversal limit and escape the sandbox, and
    # scenario files are portable — so judge "absolute" under both flavors: the
    # host's Path alone lets "/etc/passwd" through on Windows and "C:\\x" on POSIX.
    if PurePosixPath(ref_path).is_absolute() or PureWindowsPath(ref_path).is_absolute() or ref_path.startswith(("/", "\\")):
        raise ReferenceResolverError(f"Absolute reference paths are not allowed: {ref_path}")

    parent_traversals = Path(ref_path).parts.count("..")

    if parent_traversals > max_parent_traversal_depth:
        raise ReferenceResolverError(f"Reference path '{ref_path}' exceeds maximum parent traversal depth of {max_parent_traversal_depth}")

    root_path_resolved = root_path.resolve()
    base_path_resolved = base_path.resolve()

    # No CWD fallback: resolution must not depend on where the tool was launched.
    paths_to_try = [base_path]
    if root_path_resolved != base_path_resolved:
        paths_to_try.append(root_path)

    # The two rejection causes are tracked apart: a file that exists but escapes
    # the root is a sandbox refusal, not a typo, and reporting both as "not
    # found" named files the user could plainly see on disk.
    candidates: list[Path] = []
    outside_root: list[Path] = []
    for base in paths_to_try:
        try:
            resolved = (base / ref_path).resolve()
        except ValueError as e:
            # A lone surrogate the filesystem encoding cannot hold, one JSON \u
            # escape away (a NUL is refused above). The OS call's ValueError is
            # outside every caller's except block, so it escaped as a raw traceback.
            # repr keeps the message printable.
            raise ReferenceResolverError(f"Reference path {ref_path!r} is not a valid file path: {e}") from e
        if not resolved.exists():
            continue
        target = candidates if resolved.is_relative_to(root_path_resolved) else outside_root
        if resolved not in target:
            target.append(resolved)

    if not candidates:
        if outside_root:
            paths_msg = "\n  - ".join(str(path) for path in outside_root)
            raise ReferenceResolverError(
                f"Reference path '{ref_path}' resolves outside the reference root {root_path_resolved}:\n  - {paths_msg}\n"
                f"References must stay within the root; move the file inside it, or set the root explicitly (--root-path)."
            )
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

    # RFC 6901 reserves ``~`` for the two escapes below. Leaving any other
    # sequence literal would silently select a key the pointer does not name.
    if re.search(r"~(?![01])", pointer):
        raise ReferenceResolverError(f"Invalid JSON pointer: {pointer} (invalid '~' escape)")

    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
