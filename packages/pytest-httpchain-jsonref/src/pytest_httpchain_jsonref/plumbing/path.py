"""Path validation utilities for reference resolution."""

from pathlib import Path

from pytest_httpchain_jsonref.exceptions import ReferenceResolverError


class PathValidator:
    """Validates paths for security and correctness."""

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
        # Count parent traversals before resolution
        parent_traversals = sum(1 for part in Path(ref_path).parts if part == "..")

        if parent_traversals > max_parent_traversal_depth:
            raise ReferenceResolverError(f"Reference path '{ref_path}' exceeds maximum parent traversal depth of {max_parent_traversal_depth}")

        # Resolve and validate path containment
        resolved_path = (base_path / ref_path).resolve()
        root_path_resolved = root_path.resolve()

        try:
            resolved_path.relative_to(root_path_resolved)
        except ValueError:
            raise ReferenceResolverError(f"Reference path '{ref_path}' points outside allowed directory tree") from None

        return resolved_path

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
