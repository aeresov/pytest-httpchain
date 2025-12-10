"""Circular dependency tracking for reference resolution."""

from pathlib import Path
from typing import Self

from pytest_httpchain_jsonref.exceptions import ReferenceResolverError


class CircularDependencyTracker:
    """Tracks references to detect circular dependencies."""

    def __init__(self):
        self.external_refs: set[tuple[Path, str]] = set()
        self.internal_refs: set[str] = set()

    def check_external_ref(self, file_path: Path, pointer: str) -> None:
        """Check if an external reference would create a circular dependency.

        Args:
            file_path: The file being referenced
            pointer: The JSON pointer within the file

        Raises:
            ReferenceResolverError: If a circular reference is detected
        """
        ref_key = (file_path, pointer)
        if ref_key in self.external_refs:
            raise ReferenceResolverError(f"Circular reference detected: {file_path}#{pointer}")
        self.external_refs.add(ref_key)

    def check_internal_ref(self, pointer: str) -> None:
        """Check if an internal reference would create a circular dependency.

        Args:
            pointer: The JSON pointer being referenced

        Raises:
            ReferenceResolverError: If a circular reference is detected
        """
        if pointer in self.internal_refs:
            raise ReferenceResolverError(f"Circular reference detected: #{pointer}")
        self.internal_refs.add(pointer)

    def clear_external_ref(self, file_path: Path, pointer: str) -> None:
        """Clear an external reference after processing.

        Args:
            file_path: The file that was referenced
            pointer: The JSON pointer that was referenced
        """
        ref_key = (file_path, pointer)
        self.external_refs.discard(ref_key)

    def clear_internal_ref(self, pointer: str) -> None:
        """Clear an internal reference after processing.

        Args:
            pointer: The JSON pointer that was referenced
        """
        self.internal_refs.discard(pointer)

    def create_child_tracker(self) -> Self:
        """Create a child tracker that inherits current state.

        Returns:
            A new tracker with copies of the current reference sets
        """
        child = CircularDependencyTracker()
        child.external_refs = self.external_refs.copy()
        child.internal_refs = self.internal_refs.copy()
        return child
