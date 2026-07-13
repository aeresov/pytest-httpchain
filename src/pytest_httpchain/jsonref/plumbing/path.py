"""Reference path and JSON pointer helpers for reference resolution."""

from pathlib import Path

from pytest_httpchain.jsonref.exceptions import ReferenceResolverError


class RefPathHelper:
    """Stateless helpers for resolving reference file paths and parsing JSON pointers.

    Groups two related operations used when resolving ``$include``/``$merge``/``$ref``
    directives: turning a reference's file part into a validated absolute path, and
    parsing the JSON pointer part into its component keys.
    """

    @staticmethod
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
        # and `base / "/abs"` collapses to "/abs", escaping the sandbox.
        if Path(ref_path).is_absolute():
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

        # Try each base path and find the first valid existing file
        result_path = None
        for base in paths_to_try:
            resolved = (base / ref_path).resolve()
            if is_valid_and_exists(resolved):
                result_path = resolved
                break

        # If no existing file found, raise an error
        if result_path is None:
            # Provide helpful error message showing what paths were tried
            tried_paths = [str((base / ref_path).resolve()) for base in paths_to_try]
            paths_msg = "\n  - ".join(tried_paths)
            raise ReferenceResolverError(f"Reference path '{ref_path}' not found. Tried:\n  - {paths_msg}")

        return result_path

    @staticmethod
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
