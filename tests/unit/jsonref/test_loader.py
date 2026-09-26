import warnings

import pytest

from pytest_httpchain.jsonref.exceptions import ReferenceResolverError
from pytest_httpchain.jsonref.loader import load_json
from pytest_httpchain.warnings import AmbiguousReferenceWarning


@pytest.mark.parametrize(
    "case_file",
    [
        pytest.param("case_ref_self.json", id="internal"),
        pytest.param("case_ref_sibling.json", id="external"),
        pytest.param("case_ref_child.json", id="external-in-subdir"),
        pytest.param("case_ref_chain.json", id="external-chain-a-b-c"),
    ],
)
def test_ref_replaced_by_target(datadir, case_file):
    assert load_json(datadir / case_file)["target"] == {"value": 42}


@pytest.mark.parametrize("value", ["hello world", 42, 3.14159, True, False, None, [1, 2, 3]])
def test_ref_resolves_to_any_json_value(create_json_file, value):
    file = create_json_file("test.json", {"source": value, "ref": {"$ref": "#/source"}})
    resolved = load_json(file)["ref"]
    assert resolved == value
    assert type(resolved) is type(value)


class TestMerging:
    """Sibling properties merge additively into the referenced dict."""

    def test_siblings_added_to_referenced_dict(self, datadir):
        assert load_json(datadir / "case_merge_sibling.json")["result"] == {"value": 42, "extra_value": 42}

    def test_refs_resolved_inside_siblings(self, datadir):
        assert load_json(datadir / "case_merge_multi.json")["result"] == {
            "first": {"value": 42},
            "second": {"value": 42, "nested": {"extra_value": 42}},
        }

    def test_lists_concatenate_referenced_first(self, datadir):
        assert load_json(datadir / "case_merge_list.json")["result"]["items"] == ["from_sibling", "from_local"]

    def test_empty_referenced_dict_keeps_only_siblings(self, create_json_files):
        files = create_json_files({"empty.json": {}, "main.json": {"data": {"$ref": "empty.json", "extra": "value"}}})
        assert load_json(files["main.json"])["data"] == {"extra": "value"}


class TestSiblingMergePolicy:
    """No last-wins: a sibling may only repeat a value, never replace it. Null
    is a value like any other, and equality is judged in JSON terms, where
    Python's ``True == 1`` must not pass (different types AND values)."""

    @pytest.mark.parametrize(
        ("base", "sibling"),
        [(42, 99), (42, None), (None, 42), (True, 1), (False, 0), (1, True), (0, False)],
    )
    def test_differing_values_conflict(self, create_json_files, base, sibling):
        files = create_json_files({"base.json": {"value": base}, "main.json": {"data": {"$ref": "base.json", "value": sibling}}})
        with pytest.raises(ReferenceResolverError, match="Merge conflict at value"):
            load_json(files["main.json"])

    @pytest.mark.parametrize("value", [42, None, True])
    def test_equal_values_merge(self, create_json_files, value):
        files = create_json_files({"base.json": {"value": value}, "main.json": {"data": {"$ref": "base.json", "value": value}}})
        assert load_json(files["main.json"])["data"] == {"value": value}


class TestSchemaKeyPassthrough:
    """ "$schema" keys are content to the loader — never stripped at any level.

    Stripping here once corrupted a JSON Schema document pulled into a host
    via $include (its dialect declaration vanished). Tolerating "$schema" is
    the consumer's job (pytest-httpchain models drop it during validation).
    """

    def test_schema_key_preserved_in_main_document(self, create_json_file):
        doc = {"$schema": "https://example.test/schema.json", "value": 42}
        assert load_json(create_json_file("main.json", doc)) == doc

    def test_schema_key_preserved_in_included_fragment(self, create_json_files):
        schema = {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}
        files = create_json_files({"draft07.schema.json": schema, "main.json": {"verify": {"schema": {"$include": "draft07.schema.json"}}}})
        assert load_json(files["main.json"])["verify"]["schema"] == schema


def test_same_file_referenced_repeatedly_is_not_a_cycle(create_json_files):
    files = create_json_files(
        {
            "shared.json": {"common": "value"},
            "main.json": {
                "first": {"$ref": "shared.json#/common"},
                "second": {"$ref": "shared.json#/common"},
                "third": {"$ref": "shared.json"},
            },
        }
    )
    assert load_json(files["main.json"]) == {"first": "value", "second": "value", "third": {"common": "value"}}


@pytest.mark.parametrize("directive", ["$ref", "$include", "$merge"])
class TestDirectiveAliases:
    """All three spellings ($ref/$include/$merge) must behave identically.

    $include/$merge are preferred (they avoid VS Code JSON Schema conflicts);
    $ref is the legacy spelling. Parametrizing here keeps their coverage even.
    """

    def test_internal_reference(self, create_json_file, directive):
        file = create_json_file("test.json", {"source": {"value": 42}, "target": {directive: "#/source"}})
        assert load_json(file)["target"] == {"value": 42}

    def test_whole_external_file(self, create_json_files, directive):
        files = create_json_files({"external.json": {"data": "from external"}, "test.json": {"imported": {directive: "external.json"}}})
        assert load_json(files["test.json"])["imported"] == {"data": "from external"}

    def test_pointer_into_external_file(self, create_json_files, directive):
        files = create_json_files({"external.json": {"nested": {"value": 99}}, "test.json": {"target": {directive: "external.json#/nested/value"}}})
        assert load_json(files["test.json"])["target"] == 99

    def test_sibling_merge(self, create_json_file, directive):
        file = create_json_file("test.json", {"base": {"a": 1, "b": 2}, "extended": {directive: "#/base", "c": 3}})
        assert load_json(file)["extended"] == {"a": 1, "b": 2, "c": 3}

    def test_inside_array(self, create_json_file, directive):
        file = create_json_file(
            "test.json",
            {"template": {"type": "item"}, "items": [{directive: "#/template"}, {directive: "#/template"}, {"name": "custom"}]},
        )
        assert load_json(file)["items"] == [{"type": "item"}, {"type": "item"}, {"name": "custom"}]

    def test_non_string_value_raises(self, create_json_file, directive):
        """M38: a clean ReferenceResolverError, not a raw TypeError."""
        file = create_json_file("test.json", {"target": {directive: ["#/source"]}})
        with pytest.raises(ReferenceResolverError, match="must be a string"):
            load_json(file)


@pytest.mark.parametrize(
    "directives",
    [("$ref", "$include"), ("$ref", "$merge"), ("$include", "$merge"), ("$ref", "$include", "$merge")],
)
def test_multiple_directives_in_one_object_raise(create_json_files, directives):
    """M37: at most one directive per object — never silently drop the rest."""
    files = create_json_files({"external.json": {"value": 42}, "test.json": {"target": dict.fromkeys(directives, "external.json")}})
    with pytest.raises(ReferenceResolverError, match="Multiple reference directives"):
        load_json(files["test.json"])


class TestTwoCandidateLookup:
    """A relative reference is looked up first against the referencing file's
    directory, then against the root path. When a file exists under both, the
    file-relative one wins and an AmbiguousReferenceWarning is emitted."""

    def test_file_relative_wins_and_warns_when_both_exist(self, tmp_path, create_json_file):
        create_json_file("fragment.json", {"value": "root"})
        create_json_file("sub/fragment.json", {"value": "local"})
        main = create_json_file("sub/main.json", {"data": {"$ref": "fragment.json"}})

        with pytest.warns(AmbiguousReferenceWarning, match="fragment.json"):
            result = load_json(main, root_path=tmp_path)

        assert result["data"] == {"value": "local"}

    @pytest.mark.parametrize(("where", "expected"), [("fragment.json", "root"), ("sub/fragment.json", "local")])
    def test_single_candidate_is_silent(self, tmp_path, create_json_file, where, expected):
        create_json_file(where, {"value": expected})
        main = create_json_file("sub/main.json", {"data": {"$ref": "fragment.json"}})

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            result = load_json(main, root_path=tmp_path)

        assert result["data"] == {"value": expected}


def test_bad_pointer_into_external_file_names_that_file(create_json_files):
    files = create_json_files({"main.json": {"x": {"$ref": "frag.json#/missing"}}, "frag.json": {"other": 1}})
    with pytest.raises(ReferenceResolverError, match=r"Invalid JSON pointer /missing in .*frag\.json"):
        load_json(files["main.json"])
