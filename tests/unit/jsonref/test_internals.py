"""Building blocks behind load_json() whose contracts the end-to-end tests
cannot observe directly. Cycle detection and path sandboxing proper are pinned
through load_json() in test_circular_references.py and test_security.py."""

from pathlib import Path

import pytest

from pytest_httpchain.jsonref.exceptions import ReferenceResolverError
from pytest_httpchain.jsonref.plumbing.circular import CircularDependencyTracker
from pytest_httpchain.jsonref.plumbing.path import parse_json_pointer

PATH = Path("/test/file.json")


class TestCircularDependencyTracker:
    def test_child_tracker_owns_a_copy_of_external_refs(self):
        """The child starts from the parent's open external refs (so a
        cross-document cycle is caught) but refs it opens stay its own."""
        parent = CircularDependencyTracker()
        parent.check_external_ref(PATH, "/a")
        child = parent.create_child_tracker()
        child.check_external_ref(PATH, "/b")
        assert child.external_refs == {(PATH, "/a"), (PATH, "/b")}
        assert parent.external_refs == {(PATH, "/a")}

    def test_clearing_a_ref_that_is_not_open_is_a_no_op(self):
        tracker = CircularDependencyTracker()
        tracker.clear_external_ref(PATH, "/pointer")
        tracker.clear_internal_ref("/pointer")
        assert (tracker.external_refs, tracker.internal_refs) == (set(), set())


class TestParseJsonPointer:
    @pytest.mark.parametrize(
        ("pointer", "parts"),
        [
            ("", []),
            ("/", [""]),
            ("/a/b/c", ["a", "b", "c"]),
            ("/0/1/2", ["0", "1", "2"]),
            ("/key~0with~0tilde", ["key~with~tilde"]),
            ("/key~1with~1slash", ["key/with/slash"]),
            ("/a~0b~1c", ["a~b/c"]),
            pytest.param("/~01", ["~1"], id="~1-replaced-before-~0"),
        ],
    )
    def test_splits_and_unescapes(self, pointer, parts):
        assert parse_json_pointer(pointer) == parts

    def test_requires_leading_slash(self):
        with pytest.raises(ReferenceResolverError, match="must start with '/'"):
            parse_json_pointer("no/leading/slash")
