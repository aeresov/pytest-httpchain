"""body_schema: a body schema made ready to validate, its references resolved.

The failure messages a verify step gives for each problem are pinned in
test_response_steps.py; this pins what resolves, what is refused, which
dialect applies and how often a file is read.
"""

import json
import os
import pathlib
import threading
import time
import urllib.request

import jsonschema
import pytest
import referencing.exceptions
import referencing.jsonschema

from pytest_httpchain import body_schema as body_schema_module
from pytest_httpchain.body_schema import DEFAULT_DIALECT, InvalidReferencedSchema, ReferenceBounds, SchemaFile, file_body_schema, follow_pointer, inline_body_schema, schema_dialect
from pytest_httpchain.errors import SchemaFileError, SchemaPointerError

DRAFT_04 = "http://json-schema.org/draft-04/schema#"
DRAFT_07 = "http://json-schema.org/draft-07/schema#"
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"

OPENAPI = {
    "openapi": "3.1.0",
    "info": {"title": "Users", "version": "1.0.0"},
    "components": {
        "schemas": {
            "User": {
                "type": "object",
                "required": ["id", "role"],
                "properties": {"id": {"type": "integer"}, "role": {"$ref": "#/components/schemas/Role"}},
            },
            "Role": {"enum": ["admin", "user"]},
            "Users": {"type": "array", "items": {"$ref": "#/components/schemas/User"}},
            "Tagged": {"type": "object", "properties": {"tag": {"$ref": "common/tags.json#/$defs/Tag"}}},
        }
    },
}
TAGS = {"$defs": {"Tag": {"type": "string", "maxLength": 3}, "Role": {"$ref": "../openapi.json#/components/schemas/Role"}}}


def _write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def api(tmp_path):
    """An OpenAPI document with a tags file beside it, one directory down
    from the scenario's (``tmp_path``), which is also the reference root."""
    _write(tmp_path / "api" / "openapi.json", OPENAPI)
    _write(tmp_path / "api" / "common" / "tags.json", TAGS)
    return tmp_path


def _file(ref, scenario_dir, root=None, depth=None):
    return file_body_schema(SchemaFile.locate(ref, scenario_dir), ReferenceBounds(root, depth))


def _inline(schema, scenario_dir, root=None, depth=None):
    return inline_body_schema(schema, scenario_dir, ReferenceBounds(root, depth))


def _unresolved(schema, instance):
    """Why ``schema`` could not validate ``instance``: the reason for its reference."""
    with pytest.raises(referencing.exceptions.Unresolvable) as excinfo:
        schema.validate(instance)
    return schema.why_unresolvable(excinfo.value)


class TestFollowPointer:
    DOCUMENT = {"a": {"b/c": {"~d": 1}}, "list": [10, 11], "": {"empty": True}, "text": "x", "none": None, "flag": True}

    @pytest.mark.parametrize(
        ("pointer", "expected"),
        [
            pytest.param((), DOCUMENT, id="whole-document"),
            pytest.param(("a", "b/c", "~d"), 1, id="unescaped-keys"),
            pytest.param(("list", "1"), 11, id="array-index"),
            pytest.param(("",), {"empty": True}, id="empty-key"),
        ],
    )
    def test_selects(self, pointer, expected):
        assert follow_pointer(self.DOCUMENT, pointer) == expected

    @pytest.mark.parametrize(
        ("pointer", "message"),
        [
            pytest.param(("nope",), "'#' has no key 'nope'", id="missing-key"),
            # Shown escaped again: "b/c" is one key, not two.
            pytest.param(("a", "b/c", "x"), "'#/a/b~1c' has no key 'x'", id="missing-key-under-escaped"),
            pytest.param(("list", "2"), "'#/list' is an array of 2, with no item '2'", id="index-out-of-range"),
            # RFC 6901: no leading zero, no sign, no "-" (past the end).
            pytest.param(("list", "01"), "'#/list' is an array of 2, with no item '01'", id="leading-zero"),
            pytest.param(("list", "-1"), "'#/list' is an array of 2, with no item '-1'", id="negative"),
            pytest.param(("list", "-"), "'#/list' is an array of 2, with no item '-'", id="past-the-end"),
            # More digits than int() takes: refused by length, not converted.
            pytest.param(("list", "9" * 5000), "'#/list' is an array of 2, with no item", id="huge-index"),
            pytest.param(("text", "x"), "'#/text' is a string, with nothing in it to select", id="into-a-string"),
            pytest.param(("a", "b/c", "~d", "e"), "'#/a/b~1c/~0d' is a number, with nothing in it to select", id="into-a-number"),
            pytest.param(("none", "x"), "'#/none' is null, with nothing in it to select", id="into-null"),
            pytest.param(("flag", "x"), "'#/flag' is a boolean, with nothing in it to select", id="into-a-boolean"),
        ],
    )
    def test_leads_nowhere(self, pointer, message):
        with pytest.raises(SchemaPointerError, match=f"^{message}"):
            follow_pointer(self.DOCUMENT, pointer)


class TestDialect:
    UUID = {"type": "string", "format": "uuid"}

    @pytest.mark.parametrize(
        ("schema", "document", "expected"),
        [
            pytest.param({"$schema": DRAFT_07}, {"$schema": DRAFT_2020_12}, jsonschema.Draft7Validator, id="own-schema-first"),
            pytest.param({}, {"$schema": DRAFT_07}, jsonschema.Draft7Validator, id="then-the-document-root"),
            pytest.param({}, {}, DEFAULT_DIALECT, id="then-2020-12"),
            # OpenAPI 3.1's dialect is not one jsonschema knows.
            pytest.param({"$schema": "https://spec.openapis.org/oas/3.1/dialect/base"}, {"$schema": DRAFT_07}, DEFAULT_DIALECT, id="unknown-is-2020-12"),
            # A $schema that is not a string is the meta-check's to refuse, not a dialect.
            pytest.param({"$schema": ["x"]}, {"$schema": DRAFT_07}, jsonschema.Draft7Validator, id="not-a-string"),
        ],
    )
    def test_chosen(self, schema, document, expected):
        assert schema_dialect(schema, document) is expected

    def test_document_root_dialect_meta_checks_the_pointer_target(self, tmp_path):
        """Draft 7's array form of `items` is invalid under 2020-12: a target
        checked as 2020-12 because it declares no $schema of its own would be
        refused."""
        _write(tmp_path / "d7.json", {"$schema": DRAFT_07, "definitions": {"Pair": {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]}}})
        schema = _file("d7.json#/definitions/Pair", tmp_path)
        schema.check()
        schema.validate(["a", 1])
        with pytest.raises(jsonschema.ValidationError, match="'b' is not of type 'integer'"):
            schema.validate(["a", "b"])

    PAIR_DRAFT_07 = {
        "$schema": DRAFT_07,
        "definitions": {
            # Draft 7's array form of `items`, which 2020-12 refuses outright.
            "Pair": {"type": "array", "items": [{"type": "integer"}, {"type": "string"}], "additionalItems": False},
            "Wrapped": {"properties": {"pair": {"$ref": "#/definitions/Pair"}}},
        },
    }

    @pytest.mark.parametrize(
        ("how", "schema"),
        [
            pytest.param("pointer", "d7.json#/definitions/Pair", id="body-schema-pointer"),
            pytest.param("inline", {"$ref": "d7.json#/definitions/Pair"}, id="ref-from-2020-12-inline"),
            pytest.param("file", "main.json#/properties/pair", id="ref-from-2020-12-file"),
            # Reached through the document's own `#/...` reference, in turn.
            pytest.param("inline", {"$ref": "d7.json#/definitions/Wrapped", "properties": {}}, id="ref-inside-the-document"),
        ],
    )
    def test_referenced_pointer_target_takes_its_document_s_dialect(self, tmp_path, how, schema):
        """However the schema is reached, the same rule: its own `$schema`,
        else its document root's. jsonschema alone looks at the target's own
        `$schema`, and validated `Pair` reached by a `$ref` from a 2020-12
        schema as 2020-12, crashing on its `items` array."""
        _write(tmp_path / "d7.json", self.PAIR_DRAFT_07)
        _write(tmp_path / "main.json", {"properties": {"pair": {"$ref": "d7.json#/definitions/Pair"}}})
        body = _file(schema, tmp_path, tmp_path) if how != "inline" else _inline(schema, tmp_path, tmp_path)
        wrap = (lambda pair: {"pair": pair}) if "Wrapped" in str(schema) else (lambda pair: pair)
        body.validate(wrap([1, "a"]))
        with pytest.raises(jsonschema.ValidationError, match="^2 is not of type 'string'\n"):
            body.validate(wrap([1, 2]))
        with pytest.raises(jsonschema.ValidationError, match="Additional items are not allowed"):
            body.validate(wrap([1, "a", 3]))
        assert list(body.unresolvable()) == []

    def test_embedded_resource_declaring_a_dialect_keeps_the_rules(self, tmp_path):
        """A bundled document's embedded resource declares a `$schema` of its
        own. jsonschema validated it with its own class for that dialect, whose
        `$ref` knew neither the document-root dialect nor the path rules."""
        _write(tmp_path / "d7.json", self.PAIR_DRAFT_07)
        embedded = {"$schema": DRAFT_2020_12, "properties": {"pair": {"$ref": "d7.json#/definitions/Pair"}, "abs": {"$ref": "/etc/x.json"}}}
        _write(tmp_path / "bundle.json", {"properties": {"embedded": embedded}})
        schema = _file("bundle.json", tmp_path, tmp_path)
        with pytest.raises(jsonschema.ValidationError, match="^2 is not of type 'string'\n"):
            schema.validate({"embedded": {"pair": [1, 2]}})
        assert _unresolved(schema, {"embedded": {"abs": 1}})[0].startswith("$ref '/etc/x.json' is an absolute path")

    def test_referenced_target_declaring_its_own_dialect_keeps_it(self, tmp_path):
        """Its own `$schema` comes first, over its document root's."""
        _write(tmp_path / "ids.json", {"$schema": DRAFT_07, "$defs": {"Id": {**self.UUID, "$schema": DRAFT_2020_12}}})
        with pytest.raises(jsonschema.ValidationError, match="'nope' is not a 'uuid'"):
            _inline({"$ref": "ids.json#/$defs/Id"}, tmp_path).validate("nope")

    @pytest.mark.parametrize("reached", ["pointer", "ref"])
    @pytest.mark.parametrize(
        ("target", "root", "checked"),
        [
            pytest.param(UUID, None, True, id="default-2020-12"),
            pytest.param(UUID, DRAFT_07, False, id="root-draft-07"),
            pytest.param({**UUID, "$schema": DRAFT_2020_12}, DRAFT_07, True, id="target-2020-12-under-draft-07"),
        ],
    )
    def test_formats_follow_the_chosen_dialect(self, tmp_path, target, root, checked, reached):
        """`uuid` is a format from Draft 2019-09 on, so whether it is checked
        tells which dialect validated (and whose format checker ran): the same
        whether the schema is the one a pointer selects or one a `$ref`
        reaches."""
        document = {"$defs": {"Id": target}, **({"$schema": root} if root else {})}
        _write(tmp_path / "ids.json", document)
        schema = _file("ids.json#/$defs/Id", tmp_path) if reached == "pointer" else _inline({"$ref": "ids.json#/$defs/Id"}, tmp_path)
        if checked:
            with pytest.raises(jsonschema.ValidationError, match="'nope' is not a 'uuid'"):
                schema.validate("nope")
        else:
            schema.validate("nope")

    @pytest.mark.parametrize(
        ("dialect", "applied", "embedded"),
        [
            pytest.param(DRAFT_07, False, False, id="draft-07-ignores-them"),
            pytest.param("http://json-schema.org/draft-04/schema#", False, False, id="draft-04-ignores-them"),
            pytest.param(DRAFT_2020_12, True, False, id="2020-12-applies-them"),
            # A subschema declaring a `$schema` (and an `$id`) of its own.
            pytest.param(DRAFT_07, False, True, id="embedded-draft-07-ignores-them"),
            pytest.param(DRAFT_2020_12, True, True, id="embedded-2020-12-applies-them"),
        ],
    )
    def test_ref_siblings_follow_the_dialect(self, tmp_path, dialect, applied, embedded):
        """Draft 3 to 7 ignore the keywords beside a `$ref`, 2019-09 on apply
        them, in a schema given or an embedded resource. jsonschema 4.18's `extend` dropped that rule from
        the class it built, so the siblings were validated under Draft 7 too;
        CI's lowest-direct job runs this against it."""
        schema = {"$schema": dialect, "definitions": {"name": {"type": "string"}}, "properties": {"name": {"$ref": "#/definitions/name", "type": "integer"}}}
        if embedded:
            schema = {"properties": {"user": {**schema, "$id": "https://example.com/user"}}}
        body = _inline(schema, tmp_path)
        wrap = (lambda user: {"user": user}) if embedded else (lambda user: user)
        if applied:
            with pytest.raises(jsonschema.ValidationError, match="'Alice' is not of type 'integer'"):
                body.validate(wrap({"name": "Alice"}))
        else:
            body.validate(wrap({"name": "Alice"}))
        with pytest.raises(jsonschema.ValidationError, match="1 is not of type 'string'"):
            body.validate(wrap({"name": 1}))


class TestReferences:
    def test_pointer_target_resolves_references_against_the_whole_document(self, api):
        schema = _file("api/openapi.json#/components/schemas/Users", api, api)
        schema.validate([{"id": 1, "role": "admin"}])
        with pytest.raises(jsonschema.ValidationError, match="'root' is not one of \\['admin', 'user'\\]"):
            schema.validate([{"id": 1, "role": "root"}])

    def test_relative_file_reference_resolves_against_the_referring_file(self, api):
        """``common/tags.json`` is beside the OpenAPI document, not the
        scenario; and ``../openapi.json`` in it is relative to it in turn."""
        _file("api/openapi.json#/components/schemas/Tagged", api, api).validate({"tag": "abc"})
        with pytest.raises(jsonschema.ValidationError, match="'abcd' is too long"):
            _file("api/openapi.json#/components/schemas/Tagged", api, api).validate({"tag": "abcd"})
        with pytest.raises(jsonschema.ValidationError, match="'root' is not one of"):
            _file("api/common/tags.json#/$defs/Role", api, api).validate("root")

    def test_inline_schema_resolves_files_against_the_scenario_directory(self, api):
        schema = _inline({"type": "array", "items": {"$ref": "api/openapi.json#/components/schemas/User"}}, api, api)
        schema.validate([{"id": 1, "role": "user"}])
        with pytest.raises(jsonschema.ValidationError, match="'id' is a required property"):
            schema.validate([{"role": "user"}])

    def test_pointer_escapes_select_the_same_schema_referencing_resolves(self, tmp_path):
        """The pointer is followed once to pick the dialect and meta-check,
        and again by referencing to validate: both must land on the same
        schema, escapes and percent-encoding included."""
        _write(tmp_path / "paths.json", {"paths": {"/users/{id}": {"a b": {"~x": {"type": "integer"}}}}})
        schema = _file("paths.json#/paths/~1users~1%7Bid%7D/a%20b/~0x", tmp_path)
        schema.validate(1)
        with pytest.raises(jsonschema.ValidationError, match="'1' is not of type 'integer'"):
            schema.validate("1")

    # The bundling form of json-schema.org's "Structuring a complex schema":
    # an embedded `$id` resource, reached by a root-relative reference that
    # resolves against the root's `$id`.
    CUSTOMER = {
        "$id": "https://example.com/schemas/customer",
        "type": "object",
        "properties": {"address": {"$ref": "/schemas/address"}, "billing": {"$ref": "../schemas/address"}},
        "$defs": {"address": {"$id": "/schemas/address", "type": "object", "required": ["street"]}},
    }

    @pytest.mark.parametrize(
        ("how", "ref"),
        [
            pytest.param("inline", None, id="inline-root"),
            pytest.param("file", "customer.json", id="file-root"),
            # An `$id` on the pointer's way, under `$defs`.
            pytest.param("file", "bundle.json#/$defs/customer", id="pointer-target"),
            # An `$id` JSON Schema does not look for, in an OpenAPI component.
            pytest.param("file", "openapi.json#/components/schemas/Customer", id="openapi-component"),
        ],
    )
    def test_id_is_the_base_of_the_schema_given(self, tmp_path, how, ref):
        """As JSON Schema has it, and jsonschema for a schema it is given: an
        `$id`, at the root or on the pointer's way, is the base of the
        references inside it. `/schemas/address` and `../schemas/address`
        (at a depth of 0) resolve against `https://example.com/...`, so they
        name no file and keep no file path rules: at the root, they were
        refused as an absolute path and a climb, and resolved against the
        file's location, finding nothing."""
        _write(tmp_path / "customer.json", self.CUSTOMER)
        _write(tmp_path / "bundle.json", {"$defs": {"customer": self.CUSTOMER}})
        _write(tmp_path / "openapi.json", {"openapi": "3.1.0", "components": {"schemas": {"Customer": self.CUSTOMER}}})
        schema = _inline(self.CUSTOMER, tmp_path, tmp_path, 0) if how == "inline" else _file(ref, tmp_path, tmp_path, 0)
        schema.validate({"address": {"street": "x"}, "billing": {"street": "y"}})
        with pytest.raises(jsonschema.ValidationError, match="'street' is a required property"):
            schema.validate({"address": {}})
        with pytest.raises(jsonschema.ValidationError, match="'street' is a required property"):
            schema.validate({"billing": {}})
        assert list(schema.unresolvable()) == []

    # jsonschema's own lookups: the `unevaluatedProperties` of the idiomatic
    # 2020-12 composition looks its `$ref` up itself, and so does 2019-09's
    # `$recursiveRef`.
    UNEVALUATED = {
        "$schema": DRAFT_2020_12,
        "$id": "https://example.com/schemas/user.json#",
        "allOf": [{"$ref": "#/$defs/base"}],
        "properties": {"name": {"type": "string"}},
        "unevaluatedProperties": False,
        "$defs": {"base": {"properties": {"id": {"type": "integer"}}}},
    }
    RECURSIVE = {
        "$schema": "https://json-schema.org/draft/2019-09/schema",
        "$id": "https://example.com/tree#",
        "$recursiveAnchor": True,
        "type": "object",
        "properties": {"child": {"$recursiveRef": "#"}, "name": {"type": "string"}},
    }

    @pytest.mark.parametrize(
        ("document", "valid", "invalid", "error"),
        [
            pytest.param(UNEVALUATED, {"id": 1, "name": "a"}, {"id": 1, "extra": True}, "Unevaluated properties are not allowed", id="unevaluated-properties"),
            pytest.param(RECURSIVE, {"child": {"name": "a"}}, {"child": {"name": 1}}, "1 is not of type 'string'", id="recursive-ref"),
        ],
    )
    @pytest.mark.parametrize("how", ["inline", "file"])
    def test_id_with_an_empty_fragment(self, tmp_path, how, document, valid, invalid, error):
        """The meta-schemas allow an `$id` to end in `#`, which says nothing:
        the base is the `$id` without it, as referencing keys the resource.
        Kept on the base, jsonschema's own lookups missed the registry, and
        every body failed on a reference refused as a remote document, where
        `validate --deep` saw nothing wrong."""
        _write(tmp_path / "schema.json", document)
        schema = _inline(document, tmp_path, tmp_path) if how == "inline" else _file("schema.json", tmp_path, tmp_path)
        jsonschema.validators.validator_for(document)(document).validate(valid)
        schema.validate(valid)
        with pytest.raises(jsonschema.ValidationError, match=f"^{error}"):
            schema.validate(invalid)
        assert list(schema.unresolvable()) == []

    ADDRESS = {"type": "object", "required": ["street"]}

    @pytest.mark.parametrize(
        "ref",
        [
            pytest.param("openapi.json#/components/schemas/User/properties/address", id="openapi-component"),
            # The same shape where JSON Schema reads it, under `$defs`.
            pytest.param("plain.json#/$defs/User/properties/address", id="under-defs"),
            # A path's response schema: `schema` is where OpenAPI puts one, not
            # a keyword of JSON Schema's.
            pytest.param("openapi.json#/paths/~1users/get/responses/200/content/application~1json/schema/items", id="openapi-response-schema"),
        ],
    )
    def test_id_of_a_component_the_pointer_goes_into(self, tmp_path, ref):
        """The pointer passes the component on its way to the schema, so the
        component's `$id` is the base of what is inside it, `#/$defs/...`
        included, as under `$defs`: JSON Schema reads the component as a
        schema, the outermost one the selected schema is in through its
        keywords. Only the selected schema's own `$id` counted, so
        `#/$defs/Address` pointed to nothing in the OpenAPI document."""
        user = {"$id": "https://example.com/user", "type": "object", "properties": {"address": {"$ref": "#/$defs/Address"}}, "$defs": {"Address": self.ADDRESS}}
        users = {"$id": "https://example.com/users", "type": "array", "items": {"$ref": "#/$defs/Address"}, "$defs": {"Address": self.ADDRESS}}
        response = {"responses": {"200": {"content": {"application/json": {"schema": users}}}}}
        _write(tmp_path / "openapi.json", {"openapi": "3.1.0", "paths": {"/users": {"get": response}}, "components": {"schemas": {"User": user}}})
        _write(tmp_path / "plain.json", {"$defs": {"User": user}})
        schema = _file(ref, tmp_path, tmp_path)
        schema.validate({"street": "x"})
        with pytest.raises(jsonschema.ValidationError, match="'street' is a required property"):
            schema.validate({})
        assert list(schema.unresolvable()) == []

    @pytest.mark.parametrize("name", ["properties", "definitions", "patternProperties", "$defs", "allOf", "Deps"])
    def test_id_of_a_component_named_like_a_keyword(self, tmp_path, name):
        """Read from the `components/schemas` map, a component named like a
        keyword that holds schemas was a map (or array) of them, the map a
        schema: its `$id` was a property's schema, not its base, and
        `#/$defs/Dep` pointed to nothing in the OpenAPI document. Selected
        whole, it was read right; the pointer into it reads it the same."""
        deps = {"$id": "https://example.com/deps", "type": "object", "additionalProperties": {"$ref": "#/$defs/Dep"}, "$defs": {"Dep": {"type": "string"}}}
        _write(tmp_path / "openapi.json", {"openapi": "3.1.0", "components": {"schemas": {name: deps}}})
        escaped = name.replace("$", "%24")
        for ref, instance in ((f"openapi.json#/components/schemas/{escaped}", {"x": 1}), (f"openapi.json#/components/schemas/{escaped}/additionalProperties", 1)):
            schema = _file(ref, tmp_path, tmp_path)
            with pytest.raises(jsonschema.ValidationError, match="1 is not of type 'string'"):
                schema.validate(instance)
            assert list(schema.unresolvable()) == []

    def test_id_of_what_is_not_a_schema_is_not_a_base(self, tmp_path):
        """An `$id` in an object JSON Schema does not read as a schema, one
        the component is in, means nothing: `#/...` in the component is the
        document's."""
        group = {"$id": "https://example.com/group", "User": {"type": "object", "properties": {"address": {"$ref": "#/x-group/Address"}}}, "Address": self.ADDRESS}
        _write(tmp_path / "openapi.json", {"openapi": "3.1.0", "x-group": group})
        schema = _file("openapi.json#/x-group/User/properties/address", tmp_path, tmp_path)
        with pytest.raises(jsonschema.ValidationError, match="'street' is a required property"):
            schema.validate({})

    def test_root_id_of_a_document_a_reference_reaches_is_not_its_base(self, tmp_path):
        """jsonschema resolves a document it retrieves against where it is,
        whatever `$id` its root declares, so a sibling file resolves beside
        it; a reference by that `$id` still finds it. Given as the schema,
        the same document's root `$id` is its base, a remote one."""
        _write(tmp_path / "address.json", {"type": "object", "required": ["city"]})
        _write(
            tmp_path / "user.json",
            {
                "$id": "https://schemas.example.com/user.json",
                "properties": {"address": {"$ref": "address.json"}, "self": {"$ref": "https://schemas.example.com/user.json#/$defs/Id"}},
                "$defs": {"Id": {"type": "integer"}},
            },
        )
        reached = _inline({"$ref": "user.json"}, tmp_path, tmp_path)
        reached.validate({"address": {"city": "x"}, "self": 1})
        with pytest.raises(jsonschema.ValidationError, match="'city' is a required property"):
            reached.validate({"address": {}})
        with pytest.raises(jsonschema.ValidationError, match="'x' is not of type 'integer'"):
            reached.validate({"self": "x"})
        given = _file("user.json", tmp_path, tmp_path)
        assert _unresolved(given, {"address": {}})[0] == (
            "$ref 'address.json' names https://schemas.example.com/address.json, a remote document: remote references are not fetched, so keep it in a local file"
        )

    def test_openapi_component_resources(self, tmp_path):
        """Where JSON Schema does not look for them, under an OpenAPI
        `components/schemas` map: the selected component's own `$id` is the
        base of its `#/$defs/...`, an `$id` further in is one a reference
        finds (referencing's crawl does not reach it), and so is an anchor
        in a component that declares no `$id`."""
        user = {
            "$id": "https://example.com/schemas/user",
            "type": "object",
            "properties": {
                "name": {"$ref": "#/$defs/Name"},
                "address": {"$id": "https://example.com/schemas/address", "properties": {"street": {"$ref": "#/$defs/Street"}}, "$defs": {"Street": {"type": "string"}}},
            },
            "$defs": {"Name": {"type": "string"}},
        }
        tagged = {"properties": {"tag": {"$ref": "#tag"}}, "$defs": {"Tag": {"$anchor": "tag", "type": "string"}}}
        _write(tmp_path / "openapi.json", {"openapi": "3.1.0", "components": {"schemas": {"User": user, "Tagged": tagged}}})
        for component, instance in (("User", {"name": 1}), ("User", {"address": {"street": 1}}), ("Tagged", {"tag": 1})):
            schema = _file(f"openapi.json#/components/schemas/{component}", tmp_path, tmp_path)
            with pytest.raises(jsonschema.ValidationError, match="^1 is not of type 'string'"):
                schema.validate(instance)
            assert list(schema.unresolvable()) == []

    # A Draft 7 array `items`, where 2020-12 wants a schema, outside the
    # schema the pointer selects: the crawl indexing the resources fails on it.
    UNCRAWLABLE = {
        "$id": "https://example.com/u",
        "properties": {"a": {"$ref": "#/$defs/Pos"}, "b": {"$ref": "https://example.com/elsewhere"}, "tags": {"type": "array", "items": [{"type": "string"}]}},
        "$defs": {"Pos": {"type": "integer"}},
    }

    @pytest.mark.parametrize(
        ("document", "pointer"),
        [
            pytest.param({"openapi": "3.1.0", "components": {"schemas": {"U": UNCRAWLABLE}}}, "/components/schemas/U/properties", id="component"),
            pytest.param(UNCRAWLABLE, "/properties", id="document"),
        ],
    )
    def test_id_is_the_base_where_the_crawl_fails(self, tmp_path, document, pointer):
        """Kept at its `$id` uncrawled, as jsonschema keeps the schema it is
        given: `#/$defs/...` under it needs no crawl, and resolves, where it
        was refused as a remote document. A reference that misses the
        registry crawls it, and fails on what the crawl fails on."""
        _write(tmp_path / "o.json", document)
        schema = _file(f"o.json#{pointer}/a", tmp_path, tmp_path)
        schema.validate(5)
        with pytest.raises(jsonschema.ValidationError, match="'x' is not of type 'integer'"):
            schema.validate("x")
        assert list(schema.unresolvable()) == []
        # referencing's own words for the array it cannot crawl.
        reason, missing = _unresolved(_file(f"o.json#{pointer}/b", tmp_path, tmp_path), 5)
        assert (reason.startswith("$ref 'https://example.com/elsewhere' cannot be resolved: "), missing) == (True, False), reason

    def test_embedded_id_where_the_crawl_fails_on_another(self, tmp_path):
        """What the crawl fails on is the reason, as jsonschema, given the
        component, fails on it: never a remote document, which it is not."""
        user = {"$id": "https://example.com/u", "properties": {"a": {"$ref": "addr"}, "b": {"$id": "http://[x"}, "c": {"$id": "addr", "type": "integer"}}}
        _write(tmp_path / "o.json", {"openapi": "3.1.0", "components": {"schemas": {"U": user}}})
        schema = _file("o.json#/components/schemas/U/properties/a", tmp_path, tmp_path)
        assert _unresolved(schema, "x") == ("$ref 'addr' cannot be resolved: Invalid IPv6 URL", False)

    def test_pointer_under_an_id_is_relative_to_its_resource(self, tmp_path):
        """Inside a component that declares an `$id`, `#/...` is relative to
        the component, as JSON Schema has it, not to the OpenAPI document:
        the message says which schema it looked in."""
        user = {"$id": "https://example.com/schemas/user", "properties": {"role": {"$ref": "#/components/schemas/Role"}}}
        _write(tmp_path / "openapi.json", {"openapi": "3.1.0", "components": {"schemas": {"User": user, "Role": {"enum": ["admin"]}}}})
        schema = _file("openapi.json#/components/schemas/User", tmp_path, tmp_path)
        expected = ("$ref '#/components/schemas/Role' points to nothing in the schema whose $id is 'https://example.com/schemas/user'", False)
        assert _unresolved(schema, {"role": "x"}) == expected
        assert list(schema.unresolvable()) == [expected]

    def test_a_reference_under_an_id_elsewhere_is_not_a_file_reference(self, tmp_path):
        """An absolute path under an `https:` base is not refused as one: it
        names a remote document, refused as that, never a file."""
        schema = _inline({"$id": "https://example.com/schemas/user", "properties": {"a": {"$ref": "/schemas/nope"}}}, tmp_path, tmp_path, 0)
        assert _unresolved(schema, {"a": 1}) == (
            "$ref '/schemas/nope' names https://example.com/schemas/nope, a remote document: remote references are not fetched, so keep it in a local file",
            False,
        )

    def test_remote_reference_is_refused_without_a_request(self, tmp_path, monkeypatch):
        """jsonschema's default registry fetched an http(s) $ref over the
        network, with a DeprecationWarning."""

        def no_network(*args, **kwargs):
            raise AssertionError("a remote reference was fetched")

        monkeypatch.setattr(urllib.request, "urlopen", no_network)
        reason, missing = _unresolved(_inline({"$ref": "https://schemas.example.com/user.json#/$defs/Id"}, tmp_path, None), 1)
        assert (reason, missing) == (
            "$ref 'https://schemas.example.com/user.json#/$defs/Id' names https://schemas.example.com/user.json, a remote document: "
            "remote references are not fetched, so keep it in a local file",
            False,
        )

    def test_reference_that_is_neither_a_file_nor_remote(self, tmp_path):
        assert _unresolved(_inline({"$ref": "urn:example:user"}, tmp_path, None), 1) == (
            "$ref 'urn:example:user' names urn:example:user, which is not a local file, and nothing else is read",
            False,
        )

    def test_missing_file(self, tmp_path):
        assert _unresolved(_inline({"$ref": "nope.json#/$defs/X"}, tmp_path, None), 1) == (
            f"$ref 'nope.json#/$defs/X' names {tmp_path / 'nope.json'}, which does not exist",
            True,
        )

    def test_file_that_is_not_json(self, tmp_path):
        (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
        reason, missing = _unresolved(_inline({"$ref": "bad.json"}, tmp_path, None), 1)
        assert reason.startswith(f"$ref 'bad.json' names {tmp_path / 'bad.json'}, which cannot be read as JSON: Expecting property name")
        assert missing is False

    @pytest.mark.parametrize(
        ("ref", "reason"),
        [
            pytest.param("common.json#/$defs/Nope", "points to nothing: '#/$defs/Nope' is not in {common}", id="in-another-file"),
            # The reference is the pointer, so it is not repeated.
            pytest.param("#/$defs/Nope", "points to nothing in the inline schema", id="in-the-inline-schema"),
        ],
    )
    def test_pointer_to_nothing_names_the_document_not_its_contents(self, tmp_path, ref, reason):
        """referencing's own message quotes the whole document it looked in,
        an OpenAPI document included. The reference is named as written,
        whichever of its steps failed."""
        _write(tmp_path / "common.json", {"$defs": {"Big": {"description": "x" * 10_000}}})
        found, missing = _unresolved(_inline({"$ref": ref, "$defs": {}}, tmp_path, None), 1)
        assert found == f"$ref {ref!r} {reason.format(common=tmp_path / 'common.json')}"
        assert missing is False

    @pytest.mark.parametrize(
        ("schema", "reason"),
        [
            # A subschema's `$id` that is a relative `file:` URI, against a
            # base that is not a file (an `urn:`): what a reference joins to
            # it stays relative, no path to read.
            pytest.param(
                {"properties": {"a": {"$id": "urn:example:a", "properties": {"b": {"$id": "file:rel/", "$ref": "user.json"}}}}},
                "$ref 'user.json' names file:rel/user.json, which is not a local file path: URI is not absolute: 'file:rel/user.json'",
                id="relative-file-uri",
            ),
            # One JSON \u escape away. No file name can hold it, and the
            # message shows it escaped, as a UTF-8 stream cannot encode it.
            pytest.param(
                {"properties": {"a": {"$ref": "a\ud800.json"}}},
                "$ref 'a\\ud800.json' names {uri}/a\\ud800.json, which is not a local file path: 'utf-8' codec can't encode character '\\ud800'",
                id="lone-surrogate",
            ),
        ],
    )
    def test_reference_that_is_not_a_file_path(self, tmp_path, schema, reason):
        found, missing = _unresolved(_inline(schema, tmp_path), {"a": {"b": 1}})
        assert found.startswith(reason.format(uri=tmp_path.as_uri()))
        assert missing is False

    def test_pointer_missing_its_slash(self, tmp_path):
        """The mistake a JSON pointer invites: read as an anchor, and one that
        could never be."""
        assert _unresolved(_inline({"$ref": "#components/schemas/User"}, tmp_path, None), 1) == (
            "$ref '#components/schemas/User' is neither a JSON pointer (one starts with '/') nor an anchor",
            False,
        )

    def test_running_out_of_stack_in_a_lookup(self, tmp_path, monkeypatch):
        """A validation that recurses as deep as the body (``"items": {"$ref":
        "#"}``) runs out of stack wherever it is, on Python 3.14 in the middle
        of a reference's lookup: that is the body's failure, the RecursionError
        the verify step reports as one nested too deeply, not a reference that
        does not resolve. Made to happen in the lookup here, as where the stack
        gives out depends on the interpreter."""

        def out_of_stack(resolver, ref):
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(body_schema_module, "_lookup", out_of_stack)
        with pytest.raises(RecursionError):
            _inline({"items": {"$ref": "#"}}, tmp_path).validate([[1]])

    def test_anchor_that_does_not_exist(self, tmp_path):
        assert _unresolved(_inline({"$ref": "#nope"}, tmp_path, None), 1) == (
            "$ref '#nope' names the anchor 'nope', which is not in the inline schema",
            False,
        )


class TestReferenceBounds:
    """A reference to a file keeps the rules a scenario's $include path
    keeps: relative, at most so many `..`, inside the root (pytest's rootdir
    and httpchain_ref_parent_traversal_depth at runtime)."""

    @pytest.fixture
    def outside(self, tmp_path):
        """A project in ``tmp_path/project`` and a schema file beside it."""
        _write(tmp_path / "shared.json", {"$defs": {"Id": {"type": "integer"}}})
        return tmp_path / "project"

    @pytest.fixture
    def no_filesystem(self, monkeypatch):
        """Fail a test in which body_schema looks a path up on the filesystem:
        where the path is on another host, even checking that it exists opens
        a network connection (SMB, for a UNC path on Windows)."""

        def touched(*args, **kwargs):
            raise AssertionError("the filesystem was consulted")

        # The paths body_schema makes (Path.from_uri among them), and no one else's.
        untouchable = type("Untouchable", (type(pathlib.Path()),), dict.fromkeys(("exists", "resolve", "stat", "is_file", "open", "read_text", "read_bytes"), touched))
        monkeypatch.setattr(body_schema_module, "Path", untouchable)

    def test_reference_outside_the_root_is_refused(self, outside):
        reason, missing = _unresolved(_inline({"$ref": "../shared.json#/$defs/Id"}, outside, outside), 1)
        assert reason == (
            f"$ref '../shared.json#/$defs/Id' names {outside.parent / 'shared.json'}, outside the reference root {outside.resolve()}: "
            f"a schema's references must stay within it, as a scenario's $include must"
        )
        assert missing is False

    def test_symlink_out_of_the_root_is_refused(self, outside):
        """Judged on the resolved path, as $include judges it."""
        outside.mkdir()
        try:
            (outside / "link.json").symlink_to(outside.parent / "shared.json")
        except OSError:
            pytest.skip("symlinks are not available")
        reason, _ = _unresolved(_inline({"$ref": "link.json#/$defs/Id"}, outside, outside), 1)
        assert f"outside the reference root {outside.resolve()}" in reason

    def test_missing_file_outside_the_root_is_missing(self, outside):
        """A file that is not there is not found, wherever it would be."""
        assert _unresolved(_inline({"$ref": "../nope.json"}, outside, outside), 1)[1] is True

    def test_without_bounds_any_relative_reference_resolves(self, outside):
        _inline({"$ref": "../shared.json#/$defs/Id"}, outside).validate(1)

    def test_the_schema_file_itself_is_not_held_to_the_bounds(self, outside):
        """Only its references are: the file path is a scenario path like any
        other (``ssl.cert``, ``body.files``), absolute ones included, and its
        own pointers into itself need nothing retrieved."""
        _file(str(outside.parent / "shared.json") + "#/$defs/Id", outside, outside, 0).validate(1)

    @pytest.mark.parametrize(
        ("ref", "depth", "climbs"),
        [
            pytest.param("../../shared.json", 1, True, id="two-over-one"),
            pytest.param("../../shared.json", 2, False, id="two-within-two"),
            # Counted as written, as $include counts them, not as they net out.
            pytest.param("x/../../../shared.json", 2, True, id="counted-as-written"),
            # Percent-decoded, as the path they name is.
            pytest.param("%2e%2e/%2E%2E/shared.json", 1, True, id="percent-encoded"),
            pytest.param("..\\..\\shared.json", 1, True, id="backslashes"),
        ],
    )
    def test_parent_traversal_depth(self, tmp_path, ref, depth, climbs):
        """httpchain_ref_parent_traversal_depth, as for a scenario's $include."""
        _write(tmp_path / "shared.json", {"type": "integer"})
        scenario_dir = tmp_path / "a" / "b"
        schema = _inline({"$ref": ref}, scenario_dir, tmp_path, depth)
        if not climbs:
            schema.validate(1)
            return
        assert _unresolved(schema, 1) == (
            f"$ref {ref!r} exceeds the maximum parent traversal depth of {depth} (httpchain_ref_parent_traversal_depth), as a scenario's $include path would",
            False,
        )

    @pytest.mark.parametrize(
        "schema",
        [
            # Under `not`, jsonschema stops at the first error: the helper's
            # own lookup of the `$ref` was all there was, and it read the file.
            pytest.param({"not": {"unevaluatedProperties": False, "$ref": "../../shared.json"}}, id="under-not"),
            pytest.param({"unevaluatedProperties": False, "$ref": "../../shared.json"}, id="written-first"),
            pytest.param({"unevaluatedProperties": False, "allOf": [{"$ref": "../../shared.json"}]}, id="in-an-allof"),
            pytest.param({"unevaluatedItems": False, "$ref": "../../shared.json"}, id="unevaluated-items"),
        ],
    )
    def test_unevaluated_keywords_look_up_no_reference_the_rules_refuse(self, tmp_path, schema, monkeypatch):
        """``unevaluatedProperties`` and ``unevaluatedItems`` look references up
        themselves, without the rules. Applied after the other keywords, they
        look up only what `_reference` has followed, and refused, first:
        written before the `$ref`, they read the file, and its properties
        decided the step, which could pass. As `validate --deep` says."""
        _write(tmp_path / "shared.json", {"properties": {"y": {}}})
        read = []
        monkeypatch.setattr(body_schema_module, "_parsed", lambda path, stamp: read.append(path) or {"properties": {"y": {}}})
        refused = ("$ref '../../shared.json' exceeds the maximum parent traversal depth of 1 (httpchain_ref_parent_traversal_depth), as a scenario's $include path would", False)
        body = _inline(schema, tmp_path / "a" / "b", tmp_path, 1)
        assert _unresolved(body, {"x": 1, "y": 2} if "unevaluatedItems" not in schema else [1]) == refused
        assert list(body.unresolvable()) == [refused]
        assert read == []

    def test_unevaluated_keywords_apply_after_the_others(self):
        """Which the annotations they read come from: in the schema's order
        otherwise."""
        schema = {"unevaluatedItems": False, "type": "array", "unevaluatedProperties": False, "$ref": "#/$defs/a", "maxItems": 0, "$defs": {}}
        assert [keyword for keyword, _ in body_schema_module._unevaluated_last(schema)] == ["type", "$ref", "maxItems", "$defs", "unevaluatedItems", "unevaluatedProperties"]

    @pytest.mark.parametrize(
        ("ref", "kind"),
        [
            pytest.param("/etc/schemas/user.json", "an absolute path", id="posix-absolute"),
            # A network-path reference: a UNC path on Windows.
            pytest.param("//host/share/user.json", "an absolute path", id="network-path"),
            pytest.param("\\\\host\\share\\user.json", "an absolute path", id="unc-path"),
            pytest.param("C:/schemas/user.json", "an absolute path", id="drive"),
            pytest.param("file:///etc/schemas/user.json", "a file: URI", id="file-uri"),
            pytest.param("file://host/share/user.json", "a file: URI", id="file-uri-on-a-host"),
        ],
    )
    def test_absolute_reference_is_refused_before_the_filesystem(self, tmp_path, no_filesystem, ref, kind):
        """As $include refuses an absolute path, on every platform."""
        assert _unresolved(_inline({"$ref": ref}, tmp_path), 1) == (
            f"$ref {ref!r} is {kind}, which is not allowed: a reference to a file is a path relative to the file it is in, as a scenario's $include path is",
            False,
        )

    @pytest.mark.parametrize(
        "uri",
        [
            pytest.param("file://host/share/user.json", id="host"),
            # Forms urljoin does not produce, but a registry could be asked for.
            pytest.param("file:////host/share/user.json", id="empty-authority-then-unc"),
            pytest.param("file://localhost//host/share/user.json", id="localhost-then-unc"),
            pytest.param("file:///%5C%5Chost%5Cshare/user.json", id="encoded-backslashes"),
        ],
    )
    def test_file_on_another_host_is_refused_before_the_filesystem(self, no_filesystem, uri):
        """What Windows would open as a UNC path is refused before any path is
        looked up, whatever the platform."""
        retrieve = body_schema_module._LocalFiles(None, referencing.jsonschema.DRAFT202012)
        with pytest.raises(Exception, match="a file on another host: nothing is read over the network, so keep it in a local file$"):
            retrieve(uri)

    def test_id_setting_a_base_on_another_host(self, tmp_path, no_filesystem):
        """A relative reference is fine as written, but an `$id` can set a
        base on another host."""
        schema = _inline({"properties": {"a": {"$id": "file://host/share/", "$ref": "user.json"}}}, tmp_path)
        assert _unresolved(schema, {"a": 1}) == (
            "$ref 'user.json' names file://host/share/user.json, a file on another host: nothing is read over the network, so keep it in a local file",
            False,
        )

    def test_nul_is_refused(self, tmp_path, no_filesystem):
        """No file path holds one; as $include refuses it."""
        assert _unresolved(_inline({"$ref": "a%00b.json"}, tmp_path), 1) == ("$ref 'a%00b.json' contains a NUL character, which no file path can", False)

    def test_reference_by_id_is_not_a_path(self, tmp_path, no_filesystem):
        """A URI with a scheme names an `$id`, and is not held to the path
        rules: here one the schema declares."""
        declared = {"$id": "https://schemas.example.com/id.json", "type": "integer"}
        schema = _inline({"properties": {"a": {"$ref": "https://schemas.example.com/id.json"}}, "$defs": {"Id": declared}}, tmp_path)
        with pytest.raises(jsonschema.ValidationError, match="'x' is not of type 'integer'"):
            schema.validate({"a": "x"})

    def test_base_uri_is_read_where_referencing_keeps_it(self):
        """Whether a reference names a file depends on the base it resolves
        against, which referencing keeps private: pinned, so an upgrade that
        moves it fails here."""
        resolver = referencing.Registry().resolver(base_uri="file:///d/a.json")
        assert body_schema_module._base_uri(resolver) == "file:///d/a.json"
        moved = resolver.in_subresource(referencing.jsonschema.DRAFT202012.create_resource({"$id": "https://example.com/x"}))
        assert body_schema_module._base_uri(moved) == "https://example.com/x"


class TestReading:
    @pytest.fixture
    def reads(self, monkeypatch):
        """The paths `read_json_schema_file` was called for, in order."""
        seen = []
        real = body_schema_module.read_json_schema_file

        def counting(path):
            seen.append(path.name)
            return real(path)

        body_schema_module._parsed.cache_clear()
        monkeypatch.setattr(body_schema_module, "read_json_schema_file", counting)
        yield seen
        body_schema_module._parsed.cache_clear()

    def test_each_file_is_parsed_once_while_unchanged(self, api, reads):
        """Across verify steps (every iteration of a parallel stage runs its
        own), and across the items of an array each of which a $ref
        validates: referencing asks for the file once per lookup."""
        for _ in range(3):
            schema = _inline({"type": "array", "items": {"$ref": "api/openapi.json#/components/schemas/Tagged"}}, api, api)
            schema.validate([{"tag": "a"}, {"tag": "b"}, {"tag": "c"}])
            _file("api/openapi.json#/components/schemas/Users", api, api).validate([{"id": 1, "role": "user"}] * 3)
        assert sorted(reads) == ["openapi.json", "tags.json"]

    def test_a_changed_file_is_read_again(self, tmp_path, reads):
        path = _write(tmp_path / "s.json", {"type": "integer"})
        _file("s.json", tmp_path).validate(1)
        _write(path, {"type": "string", "description": "changed"})
        # A new modification time, whatever the filesystem's resolution.
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        _file("s.json", tmp_path).validate("x")
        assert reads == ["s.json", "s.json"]

    @pytest.mark.parametrize(
        ("name", "error"),
        [
            pytest.param("nope.json", "No such file or directory", id="missing"),
            # One JSON \u escape or a rendered template away: the OS call
            # raises ValueError, not OSError, which escaped as a traceback.
            pytest.param("a\x00b.json", "embedded null", id="nul"),
            pytest.param("a\ud800b.json", "surrogates not allowed", id="lone-surrogate"),
        ],
    )
    def test_unreadable_file(self, tmp_path, name, error):
        with pytest.raises(SchemaFileError, match=error):
            _file(name, tmp_path)

    def test_comments_and_trailing_commas(self, tmp_path):
        """A schema file is read as a scenario is, as JSON with comments: the
        file named, and a file a reference in it reaches."""
        (tmp_path / "user.jsonc").write_text(
            '// The user\n{\n  "type": "object",\n  "properties": {"id": {"$ref": "common.json#/$defs/Id"}}, /* its id */\n  "required": ["id",],\n}',
            encoding="utf-8",
        )
        (tmp_path / "common.json").write_text('{"$defs": {"Id": {"type": "integer"}, // positive\n},}', encoding="utf-8")
        schema = _file("user.jsonc", tmp_path)
        schema.check()
        schema.validate({"id": 1})
        with pytest.raises(jsonschema.ValidationError, match="'x' is not of type 'integer'"):
            schema.validate({"id": "x"})

    def test_unterminated_comment(self, tmp_path):
        """A syntax error at the comment's opening, in the file as written."""
        (tmp_path / "s.json").write_text('{"type": "integer"}\n/* never closed', encoding="utf-8")
        with pytest.raises(SchemaFileError, match=r"^Unterminated comment: line 2 column 1 \(char 20\)$"):
            _file("s.json", tmp_path)

    def test_a_registry_is_built_once_while_unchanged(self, api, monkeypatch):
        """A schema file's crawled registry is kept with its parse, across
        verify steps (every iteration of a parallel stage), where it was
        crawled on every one; an inline schema's, for as long as it is the
        same object, as one without templates is on every iteration."""
        body_schema_module._prepared_file.cache_clear()
        body_schema_module._prepared_inline.cache_clear()
        built = []
        real = body_schema_module._prepared
        monkeypatch.setattr(body_schema_module, "_prepared", lambda document, *args: (built.append(args[1]), real(document, *args))[1])
        inline = {"type": "array", "items": {"$ref": "api/openapi.json#/components/schemas/Tagged"}}
        for _ in range(3):
            _file("api/openapi.json#/components/schemas/Users", api, api).validate([{"id": 1, "role": "user"}])
            _inline(inline, api, api).validate([{"tag": "a"}])
        assert built == [("components", "schemas", "Users"), ()]

    def test_each_reference_is_looked_up_once_per_step(self, api, monkeypatch):
        """Not once for each item of an array it validates: every item is
        validated with the same resolver, and so is what the reference
        reaches, whose own references are looked up once too."""
        looked_up = []
        real = body_schema_module._resolve
        monkeypatch.setattr(body_schema_module, "_resolve", lambda keyword, ref, *args: (looked_up.append(ref), real(keyword, ref, *args))[1])
        for _ in range(2):
            _file("api/openapi.json#/components/schemas/Users", api, api).validate([{"id": 1, "role": "user"}] * 50)
        assert looked_up == ["#/components/schemas/User", "#/components/schemas/Role"] * 2

    def test_computed_once_however_many_threads_ask_at_once(self, tmp_path, monkeypatch, reads):
        """A parallel stage's iterations ask for the same file at the same
        time. With lru_cache, each that missed before the first was done
        parsed, meta-checked and crawled it for itself: for a large document,
        seconds each, the GIL shared among them."""
        _write(tmp_path / "s.json", {"$defs": {"A": {"type": "integer"}}})
        body_schema_module._prepared_file.cache_clear()
        body_schema_module._meta_check.cache_clear()
        slow = body_schema_module.read_json_schema_file

        def read(path):
            time.sleep(0.05)
            return slow(path)

        monkeypatch.setattr(body_schema_module, "read_json_schema_file", read)
        built = []
        prepared = body_schema_module._prepared
        monkeypatch.setattr(body_schema_module, "_prepared", lambda *args: (built.append(args[2]), prepared(*args))[1])
        start = threading.Barrier(8)
        errors = []

        def step():
            start.wait()
            try:
                schema = _file("s.json#/$defs/A", tmp_path)
                schema.check()
                schema.validate(1)
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

        threads = [threading.Thread(target=step) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert (reads, built) == (["s.json"], [("$defs", "A")])

    def test_meta_check_of_a_file_schema_is_kept_while_unchanged(self, tmp_path, monkeypatch):
        _write(tmp_path / "s.json", {"$defs": {"A": {"type": "integer"}}})
        body_schema_module._meta_check.cache_clear()
        checks = []
        real = jsonschema.Draft202012Validator.check_schema
        monkeypatch.setattr(jsonschema.Draft202012Validator, "check_schema", classmethod(lambda cls, schema, **kw: (checks.append(schema), real(schema, **kw))[1]))
        for _ in range(3):
            _file("s.json#/$defs/A", tmp_path).check()
        assert checks == [{"type": "integer"}]


class TestQuoted:
    """A reference or an `$id` as written, in a message."""

    @pytest.mark.parametrize(
        ("value", "shown"),
        [
            pytest.param("a.json", "'a.json'", id="string"),
            pytest.param(None, "null", id="null"),
            pytest.param({"type": "string"}, '{"type": "string"}', id="object"),
            pytest.param(["x" * 80], '["' + "x" * 55 + "...", id="cut-short"),
            # What a template renders into an inline schema.
            pytest.param(pathlib.Path("a"), "(a PosixPath)" if os.name != "nt" else "(a WindowsPath)", id="not-json"),
        ],
    )
    def test_shown(self, value, shown):
        assert body_schema_module._quoted(value) == shown


def test_id_problem_passes_over_what_the_meta_check_refuses():
    """A schema whose keyword holds no schemas where one should is the
    meta-check's to refuse: the walk passes over what is in it, and goes on
    to an `$id` that cannot be read elsewhere."""
    specification = referencing.jsonschema.DRAFT202012
    assert body_schema_module._id_problem({"properties": 5}, "file:///d/", specification) is None
    problem = body_schema_module._id_problem({"allOf": [{"properties": 5}, {"$id": "http://[x"}]}, "file:///d/", specification)
    assert isinstance(problem, referencing.exceptions.Unresolvable)
    assert problem.ref == "http://[x"


class TestOnce:
    """The cache the parse, the meta-check and the registries are kept in."""

    def test_failure_is_not_kept(self):
        calls = []

        @body_schema_module._once(maxsize=2)
        def compute(value):
            calls.append(value)
            if len(calls) == 1:
                raise ValueError("first")
            return value

        with pytest.raises(ValueError, match="first"):
            compute(1)
        assert (compute(1), compute(1), calls) == (1, 1, [1, 1])

    def test_least_recently_used_is_dropped(self):
        calls = []

        @body_schema_module._once(maxsize=2)
        def compute(value):
            calls.append(value)
            return value

        for value in (1, 2, 1, 3, 1, 2):
            compute(value)
        assert calls == [1, 2, 3, 2]


class TestUnresolvable:
    """The references a schema reaches, found without a body, for ``validate --deep``."""

    def test_reachable_references_that_resolve(self, api):
        assert list(_file("api/openapi.json#/components/schemas/Tagged", api, api).unresolvable()) == []
        assert list(_file("api/openapi.json#/components/schemas/Users", api, api).unresolvable()) == []

    def test_each_problem_once_in_document_order(self, tmp_path):
        _write(tmp_path / "invalid.json", {"type": 12})
        schema = _inline(
            {
                "properties": {
                    "a": {"$ref": "missing.json"},
                    "b": {"$ref": "https://schemas.example.com/b.json"},
                    "c": {"$ref": "#/$defs/Nope"},
                    "d": {"$ref": "invalid.json"},
                    # Reached twice, reported once.
                    "e": {"$ref": "missing.json"},
                    "f": {"items": {"$ref": "#/properties/a"}},
                },
                "$defs": {},
            },
            tmp_path,
            tmp_path,
        )
        problems = list(schema.unresolvable())
        assert [(reason.split(" ")[1], missing) for reason, missing in problems] == [
            ("'missing.json'", True),
            ("'https://schemas.example.com/b.json'", False),
            ("'#/$defs/Nope'", False),
            ("'invalid.json'", False),
        ]
        assert problems[2][0] == "$ref '#/$defs/Nope' points to nothing in the inline schema"
        assert problems[3][0].startswith("$ref 'invalid.json' points to an invalid JSON Schema: 12 is not valid under any of the given schemas")

    def test_walks_into_referenced_files_and_their_references(self, tmp_path):
        _write(tmp_path / "a.json", {"items": {"$ref": "sub/b.json"}})
        _write(tmp_path / "sub" / "b.json", {"properties": {"c": {"$ref": "c.json"}}})
        [(reason, missing)] = _inline({"$ref": "a.json"}, tmp_path, tmp_path).unresolvable()
        assert (reason, missing) == (f"$ref 'c.json' names {tmp_path / 'sub' / 'c.json'}, which does not exist", True)

    def test_pointer_into_an_array_by_a_name(self, tmp_path):
        """referencing reads an index into an array with int(), which raises."""
        _write(tmp_path / "doc.json", {"list": [{"type": "string"}]})
        [(reason, missing)] = _inline({"$ref": "doc.json#/list/first"}, tmp_path, tmp_path).unresolvable()
        assert (reason, missing) == ("$ref 'doc.json#/list/first' cannot be resolved: invalid literal for int() with base 10: 'first'", False)

    def test_referenced_document_declares_its_own_dialect(self, tmp_path):
        """Checked in the dialect it declares, as jsonschema validates it:
        Draft 7's array form of `items` is invalid under 2020-12."""
        _write(tmp_path / "d7.json", {"$schema": DRAFT_07, "items": [{"type": "string"}], "additionalItems": {"$ref": "missing.json"}})
        [(reason, missing)] = _inline({"$ref": "d7.json"}, tmp_path, tmp_path).unresolvable()
        assert (reason, missing) == (f"$ref 'missing.json' names {tmp_path / 'missing.json'}, which does not exist", True)

    def test_subschema_id_moves_the_base(self, tmp_path):
        """As jsonschema descends: a relative reference inside a subschema
        with an `$id` is relative to that `$id`."""
        schema = _inline({"properties": {"a": {"$id": "https://schemas.example.com/a/", "$ref": "b.json"}}}, tmp_path, tmp_path)
        [(reason, _)] = schema.unresolvable()
        assert reason.startswith("$ref 'b.json' names https://schemas.example.com/a/b.json, a remote document")

    def test_templates_are_skipped_only_where_they_are_rendered(self, tmp_path):
        """An inline schema is rendered before it is used, so a template in
        its references is known only then. A file is read as it is, and the
        runtime resolves the template's text there: so does deep."""
        _write(tmp_path / "file.json", {"properties": {"c": {"$ref": "{{ which }}.json"}}})
        schema = _inline({"properties": {"a": {"$ref": "{{ which }}.json"}, "b": {"$ref": "file.json"}}}, tmp_path, tmp_path)
        assert list(schema.unresolvable()) == [(f"$ref '{{{{ which }}}}.json' names {tmp_path / '{{ which }}.json'}, which does not exist", True)]
        assert list(_file("file.json", tmp_path, tmp_path).unresolvable()) == [(f"$ref '{{{{ which }}}}.json' names {tmp_path / '{{ which }}.json'}, which does not exist", True)]

    def test_templated_id_in_an_inline_schema_hides_what_it_bases(self, tmp_path):
        """Its references resolve against a base known once it is rendered."""
        schema = _inline({"properties": {"a": {"$id": "{{ base }}/", "properties": {"b": {"$ref": "missing.json"}}}}}, tmp_path, tmp_path)
        assert list(schema.unresolvable()) == []

    def test_dynamic_ref_is_followed_as_ref_is(self, tmp_path):
        """The runtime resolves a `$dynamicRef` through the same registry."""
        schema = _inline({"properties": {"a": {"$dynamicRef": "missing.json#/x"}}}, tmp_path, tmp_path)
        expected = (f"$dynamicRef 'missing.json#/x' names {tmp_path / 'missing.json'}, which does not exist", True)
        assert list(schema.unresolvable()) == [expected]
        assert _unresolved(schema, {"a": 1}) == expected

    def test_dynamic_ref_is_not_a_keyword_before_2020_12(self, tmp_path):
        """Draft 7 ignores it, at runtime too."""
        schema = _inline({"$schema": DRAFT_07, "properties": {"a": {"$dynamicRef": "missing.json"}}}, tmp_path, tmp_path)
        assert list(schema.unresolvable()) == []
        schema.validate({"a": 1})

    def test_malformed_id(self, tmp_path):
        """urljoin refuses it; reported, where it crashed `validate --deep`."""
        schema = _inline({"properties": {"a": {"$id": "http://[x", "properties": {"b": {"$ref": "missing.json"}}}}}, tmp_path, tmp_path)
        assert list(schema.unresolvable()) == [("$id 'http://[x' cannot be resolved: Invalid IPv6 URL", False)]

    @pytest.mark.parametrize(
        ("document", "ref"),
        [
            pytest.param({"$defs": {"A": {"$id": "http://[x", "$defs": {"B": {"type": "integer"}}}}}, "doc.json#/$defs/A/$defs/B", id="on-the-pointer-s-way"),
            pytest.param({"$id": "http://[x", "type": "integer"}, "doc.json", id="at-the-root"),
        ],
    )
    def test_malformed_id_before_the_schema(self, tmp_path, document, ref):
        """Named as one further in is, by deep and at runtime alike."""
        _write(tmp_path / "doc.json", document)
        schema = _file(ref, tmp_path, tmp_path)
        expected = ("$id 'http://[x' cannot be resolved: Invalid IPv6 URL", False)
        assert list(schema.unresolvable()) == [expected]
        with pytest.raises(referencing.exceptions.Unresolvable) as excinfo:
            schema.validate(1)
        assert schema.why_unresolvable(excinfo.value) == expected

    @pytest.mark.parametrize("how", ["inline", "file", "component"])
    def test_malformed_id_in_the_schema(self, tmp_path, how):
        """Where the crawl of the selected schema fails on it (in an OpenAPI
        component too, which the plugin adds itself): named at the start,
        whatever the body, as deep names it. jsonschema met it as it
        descended there, and urljoin's ValueError said nothing of an `$id`."""
        schema = {"properties": {"a": {"$id": "http://[x", "type": "string"}}}
        _write(tmp_path / "doc.json", schema)
        _write(tmp_path / "openapi.json", {"openapi": "3.1.0", "components": {"schemas": {"X": schema}}})
        body = {
            "inline": lambda: _inline(schema, tmp_path, tmp_path),
            "file": lambda: _file("doc.json", tmp_path, tmp_path),
            "component": lambda: _file("openapi.json#/components/schemas/X", tmp_path, tmp_path),
        }[how]()
        expected = ("$id 'http://[x' cannot be resolved: Invalid IPv6 URL", False)
        assert list(body.unresolvable()) == [expected]
        assert _unresolved(body, {"b": 1}) == expected

    def test_malformed_id_elsewhere_in_the_document(self, tmp_path):
        """Left to fail where jsonschema meets it, as it would: the selected
        schema never reaches it."""
        _write(tmp_path / "doc.json", {"$defs": {"bad": {"$id": "http://[x"}, "a": {"type": "integer"}}})
        schema = _file("doc.json#/$defs/a", tmp_path, tmp_path)
        schema.validate(1)
        assert list(schema.unresolvable()) == []

    def test_malformed_reference(self, tmp_path):
        """urljoin refuses it: not a file reference to judge, and in the same
        words at runtime as in deep."""
        schema = _inline({"properties": {"a": {"$ref": "http://[x"}}}, tmp_path, tmp_path)
        expected = ("$ref 'http://[x' cannot be resolved: Invalid IPv6 URL", False)
        assert list(schema.unresolvable()) == [expected]
        assert _unresolved(schema, {"a": 1}) == expected

    @pytest.mark.parametrize(
        ("document", "pointer", "reason"),
        [
            # urljoin's TypeError: "Cannot mix str and non-str arguments".
            pytest.param({"$id": 5, "$defs": {"a": {"type": "integer"}}}, "/$defs/a", "$id 5 is not a string: an $id is a URI reference", id="root"),
            pytest.param(
                {"$defs": {"w": {"$id": ["x"], "$defs": {"a": {"type": "integer"}}}}}, "/$defs/w/$defs/a", '$id ["x"] is not a string: an $id is a URI reference', id="on-the-way"
            ),
            # Draft 3 to 7 read it with startswith(), an AttributeError.
            pytest.param(
                {"$schema": DRAFT_07, "definitions": {"w": {"$id": 5, "definitions": {"a": {"type": "integer"}}}}},
                "/definitions/w/definitions/a",
                "$id 5 is not a string: an $id is a URI reference",
                id="draft-07",
            ),
            pytest.param(
                {"$schema": DRAFT_04, "definitions": {"w": {"id": {"type": "string"}, "definitions": {"a": {"type": "integer"}}}}},
                "/definitions/w/definitions/a",
                'id {"type": "string"} is not a string: an id is a URI reference',
                id="draft-04-id",
            ),
            pytest.param(
                {"openapi": "3.1.0", "components": {"schemas": {"U": {"$id": 5, "properties": {"a": {"type": "integer"}}}}}},
                "/components/schemas/U/properties/a",
                "$id 5 is not a string: an $id is a URI reference",
                id="openapi-component",
            ),
        ],
    )
    def test_id_that_is_not_a_string_before_the_schema(self, tmp_path, document, pointer, reason):
        """JSON Schema reads it on the way to the schema, so no body passes:
        one unresolvable reference, in the same words at runtime and in deep,
        which crashed on it with a traceback. Nothing meta-checks these
        positions, only the schema the pointer selects."""
        _write(tmp_path / "doc.json", document)
        schema = _file(f"doc.json#{pointer}", tmp_path, tmp_path)
        schema.check()
        expected = (reason, False)
        assert list(schema.unresolvable()) == [expected]
        assert _unresolved(schema, 1) == expected

    @pytest.mark.parametrize("where", ["schema", "reference-target"])
    def test_reference_that_is_not_a_string(self, tmp_path, where):
        """Where the meta-schema allows one (Draft 4's says nothing of
        `$ref`): an unresolvable reference, where it crashed on it
        ('int' object has no attribute 'partition'), in the same words at
        runtime and in deep, in a schema a reference reaches too, whose
        meta-check then passes."""
        d4 = {"$schema": DRAFT_04, "properties": {"a": {"$ref": 5}, "b": {"$ref": None}}}
        _write(tmp_path / "d4.json", d4)
        schema = _inline(d4 if where == "schema" else {"$ref": "d4.json"}, tmp_path, tmp_path)
        expected = [("$ref 5 is not a string: a reference is a URI reference", False), ("$ref null is not a string: a reference is a URI reference", False)]
        assert list(schema.unresolvable()) == expected
        assert [_unresolved(schema, {"a": 1}), _unresolved(schema, {"b": 1})] == expected

    def test_malformed_id_in_a_reference_s_target(self, tmp_path):
        """Not meta-checked before, and jsonschema met it as it descended
        there, with urljoin's ValueError: named once validating against the
        target broke down, as deep names it."""
        _write(tmp_path / "doc.json", {"properties": {"t": True, "a": {"$id": "http://[x", "type": "string"}}})
        schema = _inline({"$ref": "doc.json"}, tmp_path, tmp_path)
        expected = ("$id 'http://[x' cannot be resolved: Invalid IPv6 URL", False)
        assert list(schema.unresolvable()) == [expected]
        assert _unresolved(schema, {"a": 1}) == expected

    @pytest.mark.parametrize(
        "referenced",
        [
            pytest.param({"type": "strin"}, id="unknown-type"),
            pytest.param({"$ref": 5}, id="reference-not-a-string"),
            pytest.param([{"type": "integer"}], id="document-a-list"),
        ],
    )
    def test_invalid_target_in_deep_s_words_at_runtime(self, tmp_path, referenced):
        """Meta-checked at runtime when validating against it fails, and
        named by the reference, as deep names it, where a keyword's own crash
        was reported ('list' object has no attribute 'items')."""
        _write(tmp_path / "bad.json", referenced)
        schema = _inline({"properties": {"a": {"$ref": "bad.json"}}}, tmp_path, tmp_path)
        [(reason, missing)] = schema.unresolvable()
        assert reason.startswith("$ref 'bad.json' points to an invalid JSON Schema: ")
        with pytest.raises(InvalidReferencedSchema) as excinfo:
            schema.validate({"a": 1})
        assert (str(excinfo.value), False) == (reason, missing)

    @pytest.mark.parametrize(
        ("dialect", "walked"),
        [
            pytest.param(DRAFT_07, False, id="draft-07"),
            pytest.param(DRAFT_04, False, id="draft-04"),
            pytest.param(DRAFT_2020_12, True, id="2020-12"),
        ],
    )
    def test_ref_siblings_are_walked_where_they_apply(self, tmp_path, dialect, walked):
        """As the runtime validates them (`test_ref_siblings_follow_the_dialect`):
        under Draft 3 to 7 a `$ref` stands alone, so a reference beside it is
        never followed, and deep does not report it either."""
        schema = _inline(
            {"$schema": dialect, "definitions": {"a": {"type": "object"}}, "$ref": "#/definitions/a", "properties": {"x": {"$ref": "missing.json"}}}, tmp_path, tmp_path
        )
        missing = (f"$ref 'missing.json' names {tmp_path / 'missing.json'}, which does not exist", True)
        assert list(schema.unresolvable()) == ([missing] if walked else [])
        if walked:
            assert _unresolved(schema, {"x": 1}) == missing
        else:
            schema.validate({"x": 1})

    def test_path_rules(self, tmp_path):
        """What the runtime refuses as written, deep refuses as written."""
        schema = _inline({"properties": {"a": {"$ref": "/etc/user.json"}, "b": {"$ref": "../../x.json"}}}, tmp_path / "a" / "b", tmp_path, 1)
        assert [reason for reason, _ in schema.unresolvable()] == [
            "$ref '/etc/user.json' is an absolute path, which is not allowed: a reference to a file is a path relative to the file it is in, as a scenario's $include path is",
            "$ref '../../x.json' exceeds the maximum parent traversal depth of 1 (httpchain_ref_parent_traversal_depth), as a scenario's $include path would",
        ]
