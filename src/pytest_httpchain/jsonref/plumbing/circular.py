"""Circular dependency tracking for reference resolution."""

from pathlib import Path
from typing import Self

from pytest_httpchain.jsonref.exceptions import ReferenceResolverError


class CircularDependencyTracker:
    """Tracks references to detect circular dependencies."""

    def __init__(self):
        self.external_refs: set[tuple[Path, str]] = set()
        self.internal_refs: set[str] = set()

    def check_external_ref(self, file_path: Path, pointer: str) -> None:
        """Record an external reference, raising if it is already open."""
        ref_key = (file_path, pointer)
        if ref_key in self.external_refs:
            raise ReferenceResolverError(f"Circular reference detected: {file_path}#{pointer}")
        self.external_refs.add(ref_key)

    def check_internal_ref(self, pointer: str) -> None:
        """Record an internal reference, raising if it is already open."""
        if pointer in self.internal_refs:
            raise ReferenceResolverError(f"Circular reference detected: #{pointer}")
        self.internal_refs.add(pointer)

    def clear_external_ref(self, file_path: Path, pointer: str) -> None:
        ref_key = (file_path, pointer)
        self.external_refs.discard(ref_key)

    def clear_internal_ref(self, pointer: str) -> None:
        self.internal_refs.discard(pointer)

    def create_child_tracker(self) -> Self:
        """A tracker for descending into another document.

        External refs are inherited, so a cross-document cycle is caught along
        the chain. Internal ones are not: a pointer is only meaningful within its
        own document, and inheriting them would raise a phantom cycle for two
        documents that merely reuse a pointer string.
        """
        child = self.__class__()
        child.external_refs = self.external_refs.copy()
        child.internal_refs = set()
        return child
