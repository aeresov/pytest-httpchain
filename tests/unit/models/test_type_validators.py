"""Unit tests for custom type validators in types.py.

Each validated type is exercised through a ``TypeAdapter``: accepted values
pass through unchanged, rejected ones raise with the validator's own message.
"""

import json
import pickle
import re
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest
from pydantic import TypeAdapter, ValidationError

from pytest_httpchain.models.types import (
    Base64String,
    FunctionImportName,
    GraphQLQuery,
    JMESPathExpression,
    JSONSchemaInline,
    PartialTemplateStr,
    RegexPattern,
    SchemaFileRef,
    SchemaFileRefStr,
    TemplateExpression,
    VariableName,
    VarsNamespace,
    XMLString,
    check_json_schema,
    convert_dict_to_namespace,
    convert_namespace_items_to_dict,
    convert_namespace_to_dict,
    json_schema_validator_class,
    parse_schema_file_ref,
)
from tests.unit.helpers import BEYOND_RECURSION_LIMIT, LOADABLE_BUT_DEEP, nested


def validate(annotated_type, value):
    return TypeAdapter(annotated_type).validate_python(value)


class TestVariableName:
    @pytest.mark.parametrize("value", ["foo", "bar_baz", "_private", "CamelCase", "var1", "item_2"])
    def test_valid(self, value):
        assert validate(VariableName, value) == value

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("1invalid", "Invalid Python variable name"),
            ("foo-bar", "Invalid Python variable name"),
            ("foo.bar", "Invalid Python variable name"),
            ("with space", "Invalid Python variable name"),
            ("class", "Python keyword is used"),
            ("def", "Python keyword is used"),
            ("return", "Python keyword is used"),
            # Soft keywords too.
            ("match", "Python keyword is used"),
            ("case", "Python keyword is used"),
        ],
    )
    def test_invalid(self, value, message):
        with pytest.raises(ValidationError, match=message):
            validate(VariableName, value)


class TestFunctionImportName:
    @pytest.mark.parametrize("value", ["module:func", "package.module:func", "a.b.c.d:my_func"])
    def test_valid(self, value):
        assert validate(FunctionImportName, value) == value

    def test_bare_name_rejected_with_hint(self):
        """A module-less name fails validation with the actionable format hint
        (previously it validated and only failed at runtime import)."""
        with pytest.raises(ValidationError, match="Module path is required"):
            validate(FunctionImportName, "my_function")

    @pytest.mark.parametrize("value", ["123invalid", "module:123func", "module::func"])
    def test_invalid_format(self, value):
        with pytest.raises(ValidationError, match="Invalid function name format"):
            validate(FunctionImportName, value)


class TestJMESPathExpression:
    @pytest.mark.parametrize(
        "value",
        ["data", "data.value", "items[0]", "data.items[*].name", "response.body.users[?age > `18`]", "items | [0]"],
    )
    def test_valid(self, value):
        assert validate(JMESPathExpression, value) == value

    @pytest.mark.parametrize("value", ["[invalid", "data..value"])
    def test_invalid(self, value):
        with pytest.raises(ValidationError, match="Invalid JMESPath expression"):
            validate(JMESPathExpression, value)


class TestRegexPattern:
    @pytest.mark.parametrize("value", [r"\d+", r"[a-z]+", "hello", r"^\d{3}-\d{4}$", r"(?:https?://)?[\w.-]+"])
    def test_valid(self, value):
        assert validate(RegexPattern, value) == value

    @pytest.mark.parametrize("value", ["[invalid", "(unclosed"])
    def test_invalid(self, value):
        with pytest.raises(ValidationError, match="Invalid regular expression"):
            validate(RegexPattern, value)


class TestXMLString:
    @pytest.mark.parametrize(
        "value",
        [
            "<root>content</root>",
            '<user id="1" active="true">John</user>',
            "<root><child><grandchild>value</grandchild></child></root>",
        ],
    )
    def test_valid(self, value):
        assert validate(XMLString, value) == value

    def test_valid_with_namespaces_from_file(self, datadir):
        xml = (datadir / "xml_with_namespace.xml").read_text()
        assert validate(XMLString, xml) == xml

    @pytest.mark.parametrize("value", ["<root>content", "<root>content</other>", "not xml at all"])
    def test_invalid(self, value):
        with pytest.raises(ValidationError, match="Invalid XML"):
            validate(XMLString, value)


class TestGraphQLQuery:
    @pytest.mark.parametrize(
        "value",
        [
            "{ user { id name } }",
            "query GetUser { user { id name email } }",
            "mutation CreateUser($name: String!) { createUser(name: $name) { id } }",
            "query GetUsers($limit: Int) { users(limit: $limit) { id name } }",
        ],
    )
    def test_valid(self, value):
        assert validate(GraphQLQuery, value) == value

    def test_valid_with_fragments_from_file(self, datadir):
        query = (datadir / "graphql_with_fragments.graphql").read_text()
        assert validate(GraphQLQuery, query) == query

    @pytest.mark.parametrize("value", ["{ user { id name }", "not a graphql query"])
    def test_invalid(self, value):
        with pytest.raises(ValidationError, match="Invalid GraphQL query"):
            validate(GraphQLQuery, value)


class TestTemplateExpression:
    """A complete ``{{ expr }}`` — nothing outside the braces."""

    @pytest.mark.parametrize("value", ["{{ value }}", "{{ foo }}", "{{ a + b }}", "{{ user.name }}", "{{ name | upper }}"])
    def test_valid(self, value):
        assert validate(TemplateExpression, value) == value

    @pytest.mark.parametrize("value", ["prefix {{ value }}", "{{ value }} suffix", "just a string"])
    def test_invalid(self, value):
        with pytest.raises(ValidationError, match="Must be a complete template expression"):
            validate(TemplateExpression, value)


class TestPartialTemplateStr:
    """At least one non-empty ``{{ expr }}`` anywhere in the string."""

    @pytest.mark.parametrize("value", ["{{ value }}", "Hello {{ name }}!", "prefix {{ value }} suffix", "{{ first }} and {{ second }}"])
    def test_valid(self, value):
        assert validate(PartialTemplateStr, value) == value

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("no template here", "Must contain at least one template expression"),
            ("{{  }}", "Template expression cannot be empty"),
        ],
    )
    def test_invalid(self, value, message):
        with pytest.raises(ValidationError, match=message):
            validate(PartialTemplateStr, value)


class TestBase64String:
    @pytest.mark.parametrize("value", ["SGVsbG8=", "dGVzdA==", ""])
    def test_valid(self, value):
        assert validate(Base64String, value) == value

    @pytest.mark.parametrize("value", ["not-valid-base64!!!", pytest.param("SGVsbG8", id="missing-padding")])
    def test_invalid(self, value):
        with pytest.raises(ValidationError, match="Invalid base64 encoding"):
            validate(Base64String, value)


class TestJSONSchemaInline:
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param({"type": "string"}, id="simple"),
            pytest.param(
                {"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"}}, "required": ["name"]},
                id="object",
            ),
            pytest.param(
                {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object", "properties": {"id": {"type": "integer"}}},
                id="declared-dialect",
            ),
        ],
    )
    def test_valid(self, value):
        assert validate(JSONSchemaInline, value) == value

    def test_valid_complex_schema_from_file(self, datadir):
        schema = json.loads((datadir / "object_schema.json").read_text())
        assert validate(JSONSchemaInline, schema) == schema

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            pytest.param({"type": "not_a_type"}, "Invalid JSON Schema", id="unknown-type"),
            pytest.param({"properties": "not_an_object"}, "Invalid JSON Schema", id="bad-structure"),
            # Not a SchemaError: jsonschema itself fails on the non-string dialect.
            pytest.param({"$schema": 123}, "JSON Schema validation error", id="non-string-dialect"),
        ],
    )
    def test_invalid(self, value, message):
        with pytest.raises(ValidationError, match=message):
            validate(JSONSchemaInline, value)


class TestCheckJsonSchema:
    @pytest.mark.parametrize(
        "schema",
        [
            pytest.param({"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}, id="draft-07"),
            pytest.param({"$schema": "http://json-schema.org/draft-04/schema#", "type": "string"}, id="draft-04"),
            pytest.param({"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "array", "items": {"type": "string"}}, id="2020-12"),
            pytest.param({"type": "object", "properties": {"id": {"type": "integer"}}}, id="no-dialect"),
        ],
    )
    def test_valid_schema_passes(self, schema):
        check_json_schema(schema)

    def test_invalid_schema_raises(self):
        with pytest.raises(jsonschema.SchemaError):
            check_json_schema({"type": "invalid_type"})

    def test_schema_without_dialect_uses_draft_2020_12(self):
        """The fallback is the dialect ``jsonschema.validate`` picks, so the
        meta-check and instance validation agree."""
        assert json_schema_validator_class({"type": "object"}) is jsonschema.Draft202012Validator


class TestSchemaFileRef:
    """A ``body.schema`` file reference: a local file, then an RFC 6901 JSON
    pointer in URI fragment form after its first ``#``."""

    @pytest.mark.parametrize(
        ("ref", "expected"),
        [
            pytest.param("schemas/user.json", SchemaFileRef("schemas/user.json", "", ()), id="no-pointer"),
            pytest.param("user.json#", SchemaFileRef("user.json", "", ()), id="empty-fragment-is-the-whole-document"),
            pytest.param(
                "./openapi.json#/components/schemas/User",
                SchemaFileRef("./openapi.json", "/components/schemas/User", ("components", "schemas", "User")),
                id="openapi-component",
            ),
            # Percent-decoded first, then split, then unescaped: %2F is a
            # separator, ~1 a slash in a key, and ~01 is "~1", not "/".
            pytest.param("a.json#/paths/~1users~1{id}/get", SchemaFileRef("a.json", "/paths/~1users~1{id}/get", ("paths", "/users/{id}", "get")), id="escaped-slash"),
            pytest.param("a.json#/~0x/~01", SchemaFileRef("a.json", "/~0x/~01", ("~x", "~1")), id="escaped-tilde"),
            pytest.param("a.json#/a%20b/c%2Fd/%C3%A9", SchemaFileRef("a.json", "/a%20b/c%2Fd/%C3%A9", ("a b", "c", "d", "é")), id="percent-decoded"),
            # A Path would fold these: "/" is the empty key, "//x" two keys.
            pytest.param("a.json#/", SchemaFileRef("a.json", "/", ("",)), id="empty-key"),
            pytest.param("a.json#//x/", SchemaFileRef("a.json", "//x/", ("", "x", "")), id="empty-keys"),
            pytest.param("a.json#/items/0", SchemaFileRef("a.json", "/items/0", ("items", "0")), id="array-index"),
            # Only the first '#' splits: the pointer may hold one.
            pytest.param("a.json#/a#b", SchemaFileRef("a.json", "/a#b", ("a#b",)), id="hash-in-pointer"),
            # A one-letter scheme is a Windows drive.
            pytest.param("C:\\schemas\\x.json#/a", SchemaFileRef("C:\\schemas\\x.json", "/a", ("a",)), id="windows-drive"),
            # A colon is a path's, unless it starts a URI: these loaded as
            # paths before the field took pointers, and still do.
            pytest.param("schemas:v1/user.json", SchemaFileRef("schemas:v1/user.json", "", ()), id="colon-in-a-directory"),
            pytest.param("user-2024-01-01T10:00.json", SchemaFileRef("user-2024-01-01T10:00.json", "", ()), id="colon-in-a-name"),
            pytest.param("urn:x.json", SchemaFileRef("urn:x.json", "", ()), id="scheme-like-without-authority"),
            # `./` keeps a path that starts like a URI a path.
            pytest.param("./http:x/user.json", SchemaFileRef("./http:x/user.json", "", ()), id="dot-slash-before-a-scheme"),
        ],
    )
    def test_parsed(self, ref, expected):
        assert parse_schema_file_ref(ref) == expected
        assert validate(SchemaFileRefStr, ref) == ref

    @pytest.mark.parametrize(
        ("ref", "message"),
        [
            pytest.param("https://example.com/openapi.json#/components", "remote schemas are not fetched", id="https"),
            pytest.param("HTTP://example.com/s.json", "remote schemas are not fetched", id="http-any-case"),
            pytest.param("file:///srv/s.json", "a local file path, not a URI", id="file-uri"),
            pytest.param("File:s.json", "a local file path, not a URI", id="file-uri-without-slashes"),
            pytest.param("ftp://example.com/s.json", "a local file path, not a URI", id="other-scheme-with-authority"),
            pytest.param("https:s.json", "remote schemas are not fetched", id="https-without-slashes"),
            # The first '#' ends the path, so a file whose name holds one
            # cannot be named (rename it).
            pytest.param("schemas/c#1.json", "must be a JSON pointer starting with '/'", id="hash-in-a-file-name"),
            pytest.param("#/components/schemas/User", "must name a file before its '#'", id="pointer-only"),
            pytest.param("", "must name a file before its '#'", id="empty"),
            pytest.param("a.json#User", "must be a JSON pointer starting with '/'", id="plain-name-anchor"),
            pytest.param("a.json#/a~2b", "must be followed by 0 (for '~') or 1 (for '/')", id="bad-escape"),
            pytest.param("a.json#/a~", "must be followed by 0 (for '~') or 1 (for '/')", id="trailing-tilde"),
            # A tilde spelled %7E is a tilde once decoded: the escape rule holds.
            pytest.param("a.json#/a%7E2", "must be followed by 0 (for '~') or 1 (for '/')", id="encoded-bad-escape"),
            pytest.param("a.json#/%FF", "percent-encoding is not UTF-8", id="not-utf8"),
        ],
    )
    def test_refused(self, ref, message):
        with pytest.raises(ValueError, match=re.escape(message)):
            parse_schema_file_ref(ref)
        with pytest.raises(ValidationError, match=re.escape(message)):
            validate(SchemaFileRefStr, ref)

    def test_path_object_stands_for_its_text(self):
        """A template renders a fixture's Path as it is, which the field took
        when it was a Path."""
        assert validate(SchemaFileRefStr, Path("schemas") / "user.json") == str(Path("schemas") / "user.json")

    def test_template_is_left_to_the_template_branch(self):
        with pytest.raises(ValidationError, match="contains a template expression at position 8"):
            validate(SchemaFileRefStr, "schemas/{{ name }}.json")


class TestConvertNamespaceToDict:
    """The validator behind JSON bodies, inline schemas and jmespath operands."""

    def test_converts_at_every_depth_keeping_key_order(self):
        value = {"b": 1, "a": SimpleNamespace(z=[1, SimpleNamespace(y=2)], x={}), "c": [{"d": SimpleNamespace()}]}
        converted = convert_namespace_to_dict(value)
        assert converted == {"b": 1, "a": {"z": [1, {"y": 2}], "x": {}}, "c": [{"d": {}}]}
        assert list(converted) == ["b", "a", "c"]
        assert list(converted["a"]) == ["z", "x"]

    def test_value_nested_past_the_recursion_limit(self):
        """Iterative: the loader accepts any depth, and a recursive walk overflowed."""
        converted = convert_namespace_to_dict(nested(SimpleNamespace(leaf=1), BEYOND_RECURSION_LIMIT))
        # Unwrapped level by level: == on it recurses in C, which Windows caps
        # at 3,000 levels.
        for level in reversed(range(BEYOND_RECURSION_LIMIT)):
            converted = converted[0] if level % 2 else converted["k"]
        assert converted == {"leaf": 1}

    def test_shared_value_is_copied_where_it_occurs(self):
        shared = [SimpleNamespace(k=1)]
        converted = convert_namespace_to_dict({"a": shared, "b": shared})
        assert converted == {"a": [{"k": 1}], "b": [{"k": 1}]}
        assert converted["a"] is not converted["b"]

    def test_value_that_contains_itself_is_refused(self):
        """A plain loop would never end on it (the recursive walk overflowed)."""
        cyclic: list = []
        cyclic.append(cyclic)
        with pytest.raises(ValueError, match="contains itself"):
            convert_namespace_to_dict(cyclic)


class TestVarsNamespace:
    """What a `vars` object is to a template: read by attribute, as a
    SimpleNamespace always was, and by key, as a dict saved from a response is."""

    @staticmethod
    def _user() -> Any:
        return convert_dict_to_namespace({"name": "Alice", "Content-Type": "json", "roles": [{"id": 1}], "address": {"city": "Oslo"}})

    def test_every_object_at_every_depth_is_one(self):
        """Nested objects and objects in lists too, so a template reads
        ``user['address']['city']`` and ``user.roles[0]['id']``."""
        user = self._user()
        assert type(user) is VarsNamespace
        assert type(user["address"]) is VarsNamespace
        assert type(user.roles[0]) is VarsNamespace
        assert (user["address"]["city"], user.roles[0]["id"]) == ("Oslo", 1)

    def test_mapping_protocol_reads_the_keys_in_their_order(self):
        user = self._user()
        assert user["Content-Type"] == "json"
        assert "name" in user
        assert "nick" not in user
        assert len(user) == 4
        assert list(user) == ["name", "Content-Type", "roles", "address"]
        assert list(user.keys()) == ["name", "Content-Type", "roles", "address"]
        assert list(user.values())[:2] == ["Alice", "json"]
        assert list(user.items())[:2] == [("name", "Alice"), ("Content-Type", "json")]
        assert (user.get("name"), user.get("nick"), user.get("nick", "anon")) == ("Alice", None, "anon")

    def test_attribute_access_is_unchanged(self):
        assert self._user().name == "Alice"

    def test_missing_key_raises_key_error(self):
        """The template engine names the key in its own message."""
        with pytest.raises(KeyError, match="nick"):
            self._user()["nick"]

    def test_key_named_like_a_method_is_data(self):
        """As an attribute the data wins, as it always did, and shadows the
        method: ``order.items()`` calls the data. Every other form reads the
        key, since the methods read the instance's dict, never an attribute."""
        order = convert_dict_to_namespace({"items": [1, 2], "keys": "k", "get": "g", "values": 0})
        assert (order.items, order.keys, order.get, order.values) == ([1, 2], "k", "g", 0)
        assert order["items"] == [1, 2]
        assert list(order) == ["items", "keys", "get", "values"]
        assert len(order) == 4
        assert "items" in order
        with pytest.raises(TypeError, match="not callable"):
            order.items()
        # The class's own methods still work around the shadowing.
        assert list(VarsNamespace.items(order)) == [("items", [1, 2]), ("keys", "k"), ("get", "g"), ("values", 0)]

    def test_underscore_and_dunder_keys_are_data_only(self):
        """A key is data: ``_id`` reads by key. The class's attributes are
        never reached by key, whatever the key."""
        doc = convert_dict_to_namespace({"_id": 7, "__class__": "shadow"})
        assert doc["_id"] == 7
        assert doc["__class__"] == "shadow"
        assert type(doc) is VarsNamespace
        with pytest.raises(KeyError):
            convert_dict_to_namespace({})["__class__"]

    def test_special_methods_are_the_class_s_whatever_the_keys(self):
        """Python looks special methods up on the class, so data cannot
        replace them."""
        odd = convert_dict_to_namespace({"__getitem__": 1, "__len__": 2, "__iter__": 3})
        assert (odd["__len__"], len(odd), list(odd)) == (2, 3, ["__getitem__", "__len__", "__iter__"])

    def test_equality_is_a_simple_namespace_s(self):
        """Equal to a plain SimpleNamespace of the same members, as before; a
        dict stays unequal, as it was."""
        user = convert_dict_to_namespace({"a": 1, "b": {"c": 2}})
        assert user == SimpleNamespace(a=1, b=SimpleNamespace(c=2))
        assert user == convert_dict_to_namespace({"a": 1, "b": {"c": 2}})
        assert user != convert_dict_to_namespace({"a": 1, "b": {"c": 3}})
        assert user != {"a": 1, "b": {"c": 2}}

    @pytest.mark.parametrize(("value", "expected"), [({}, False), ({"a": 0}, True)], ids=["empty", "non-empty"])
    def test_truth_is_a_dict_s(self, value, expected):
        """An empty object is false, as ``{}`` saved from a response is."""
        assert bool(convert_dict_to_namespace(value)) is expected

    def test_repr_is_a_simple_namespace_s(self):
        """Interpolated into text, an object reads as it always has."""
        ns = convert_dict_to_namespace({"a": 1, "Content-Type": "x", "b": {"c": [1]}, "": "unnamed"})
        assert repr(ns) == repr(SimpleNamespace(**{"a": 1, "Content-Type": "x", "b": SimpleNamespace(c=[1]), "": "unnamed"}))
        assert str(ns) == "namespace(a=1, Content-Type='x', b=namespace(c=[1]))"

    def test_repr_of_an_object_that_contains_itself(self):
        loop = VarsNamespace(a=1)
        loop.self = loop
        assert repr(loop) == "namespace(a=1, self=namespace(...))"

    @staticmethod
    def _both(build: Any) -> tuple[Any, Any]:
        """``build(namespace_type)`` for this class and for SimpleNamespace,
        whose C repr is the text to match."""
        return build(VarsNamespace), build(SimpleNamespace)

    @staticmethod
    def _list_cycle(ns: Any) -> Any:
        items: list[Any] = [1]
        obj = ns(items=items)
        items.extend([items, obj])
        return obj

    @staticmethod
    def _dict_cycle(ns: Any) -> Any:
        data: dict[str, Any] = {}
        obj = ns(data=data)
        data.update(data=data, obj=obj)
        return obj

    @staticmethod
    def _shared(ns: Any) -> Any:
        shared = ns(x=1)
        return ns(a=shared, b=shared, c=[shared, shared])

    @pytest.mark.parametrize(
        "build",
        [
            # A template can render any of these into an object.
            pytest.param(lambda ns: ns(d={"k": ns(a=1), 2: (ns(),)}, one=(1,), none=(), pair=(1, "b"), nested=[[], [{}]]), id="rendered-dicts-and-tuples"),
            pytest.param(_list_cycle, id="list-that-contains-itself"),
            pytest.param(_dict_cycle, id="dict-that-contains-itself"),
            # Met again, but not inside itself: written again, as the C reprs do.
            pytest.param(_shared, id="shared-not-a-cycle"),
            pytest.param(lambda ns: ns(**{"a": b"x", "f": 1.5, "n": None, "t": True, "s": "it's"}), id="scalars"),
        ],
    )
    def test_repr_writes_the_c_repr_s_text(self, build):
        ours, plain = self._both(build)
        assert repr(ours) == repr(plain)

    def test_repr_of_an_object_nested_hundreds_deep(self):
        """SimpleNamespace's C repr wrote an object the loader accepts at this
        depth; a Python repr recursing per level spent three frames on each
        and overflowed, so a verify expression rendering one failed as a bare
        RecursionError and text interpolating one failed the stage."""
        loaded = convert_dict_to_namespace({"deep": nested("x", LOADABLE_BUT_DEEP)})

        def build(ns: Any) -> Any:
            value: Any = "x"
            for level in range(LOADABLE_BUT_DEEP):
                value = [value] if level % 2 else ns(k=value)
            return ns(deep=value)

        ours, plain = self._both(build)
        assert repr(loaded) == repr(ours) == repr(plain)

    def test_repr_far_past_the_recursion_limit(self):
        value: Any = "x"
        for _ in range(BEYOND_RECURSION_LIMIT):
            value = VarsNamespace(k=[value])
        assert repr(value).startswith("namespace(k=[namespace(k=[")

    def test_repr_through_another_object_s_repr(self):
        """An object written by its own repr that holds the namespace writing
        it: the guard is the thread's, not the loop's."""

        class Box:
            def __init__(self, value: Any) -> None:
                self.value = value

            def __repr__(self) -> str:
                return f"Box({self.value!r})"

        obj = VarsNamespace(a=1)
        obj.box = Box(obj)
        assert repr(obj) == "namespace(a=1, box=Box(namespace(...)))"

    def test_repr_that_raises_leaves_no_guard_behind(self):
        """Else the object would read ``namespace(...)`` from then on."""

        class Unwritable:
            def __repr__(self) -> str:
                raise ValueError("no repr")

        obj = VarsNamespace(a=1)
        obj.bad = Unwritable()
        with pytest.raises(ValueError, match="no repr"):
            repr(obj)
        del obj.bad
        assert repr(obj) == "namespace(a=1)"

    def test_pickles_with_its_type(self):
        """Namespaces may cross into xdist's reports or a process pool."""
        user = self._user()
        copy = pickle.loads(pickle.dumps(user))
        assert copy == user
        assert (type(copy), type(copy.address), type(copy.roles[0])) == (VarsNamespace, VarsNamespace, VarsNamespace)

    def test_shared_by_threads(self):
        """Parallel iterations read one object at once: the methods only read."""
        user = self._user()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: (user["name"], list(user), dict(user.items())["Content-Type"]), range(64)))
        assert results == [("Alice", ["name", "Content-Type", "roles", "address"], "json")] * 64

    def test_is_a_simple_namespace_and_a_mapping(self):
        user = self._user()
        assert isinstance(user, SimpleNamespace)
        assert isinstance(user, Mapping)
        assert dict(user) == {"name": "Alice", "Content-Type": "json", "roles": user.roles, "address": user.address}

    @pytest.mark.parametrize("annotated_type", [list[Any], list[str], tuple[str, ...], set[str]], ids=["list", "list-str", "tuple", "set"])
    def test_refused_where_a_list_goes_as_a_dict_is(self, annotated_type):
        """pydantic's lax list takes any iterable but a mapping: as a plain
        iterable, an object rendered where a list goes (a parameter's values)
        was taken for the list of its keys."""
        with pytest.raises(ValidationError, match="Input should be a valid (list|tuple|set)"):
            validate(annotated_type, self._user())

    def test_taken_where_an_object_goes_as_a_dict_is(self):
        assert validate(dict[str, Any], convert_dict_to_namespace({"a": 1})) == {"a": 1}

    def test_converts_to_json_data_as_a_simple_namespace(self):
        user = self._user()
        assert convert_namespace_to_dict(user) == {"name": "Alice", "Content-Type": "json", "roles": [{"id": 1}], "address": {"city": "Oslo"}}
        assert json.loads(json.dumps(convert_namespace_to_dict(user))) == convert_namespace_to_dict(user)

    def test_one_object_is_not_a_sequence_of_items(self):
        """Iterable over its keys, but a mapping: not iterated for them."""
        user = self._user()
        assert convert_namespace_items_to_dict(user) is user
        assert convert_namespace_items_to_dict((user,)) == [dict(vars(user))]
