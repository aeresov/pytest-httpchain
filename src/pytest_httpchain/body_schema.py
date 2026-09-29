"""A verify step's ``body.schema``, made ready to validate a response body.

A schema is inline, or a local file with an optional JSON pointer into it
(``openapi.json#/components/schemas/User``, `models.parse_schema_file_ref`).
Either way its ``$ref``s resolve through one ``referencing`` registry: across
the whole document the schema is in (``#/components/schemas/Address`` from
inside the schema the pointer selects), and into other local files, relative
to the file the reference is written in, or to the scenario's directory for
an inline schema. Nothing is fetched: a remote reference fails, and so does a
reference to a file that breaks the rules a scenario's ``$include`` path
keeps (`ReferenceBounds`).

Shared by the runtime (`response_steps`), which validates with a
`BodySchema`, and ``validate --deep``, which walks every reference one
reaches (`BodySchema.unresolvable`), so both resolve the same way.
"""

import collections
import functools
import json
import os
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, cast
from urllib.parse import unquote, urljoin, urlsplit

import attrs
import jsonschema
import jsonschema.validators
import referencing
import referencing.exceptions
import referencing.jsonschema
from jsonschema_specifications import REGISTRY as _META_SCHEMAS

from pytest_httpchain.errors import SchemaFileError, SchemaPointerError
from pytest_httpchain.models import json_schema_validator_class, parse_schema_file_ref
from pytest_httpchain.templates import needs_rendering
from pytest_httpchain.utils import read_json_schema_file, resolve_scenario_path, schema_error_text

type Dialect = type[jsonschema.protocols.Validator]

# The dialect of a schema that declares none, as `json_schema_validator_class`
# has it.
DEFAULT_DIALECT: Dialect = jsonschema.Draft202012Validator

# An array index in a JSON pointer: 0, or digits without a leading zero (RFC 6901).
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")

# The keywords that reference another schema by a URI, and so may name a file.
# (Draft 2019-09's `$recursiveRef` is always "#", the resource it is in.)
_REFERENCE_KEYWORDS = ("$ref", "$dynamicRef")


@dataclass(frozen=True, slots=True)
class ReferenceBounds:
    """What a body schema's references to files are held to, as a scenario's
    ``$include`` paths are (`jsonref.plumbing.path.validate_ref_path`):
    written as a relative path, with at most ``max_parent_traversal`` ``..``
    segments, to a file inside ``root``. None leaves that one out, for a
    caller that has no root or limit; an absolute path is refused always.

    The runtime passes pytest's rootdir and
    ``httpchain_ref_parent_traversal_depth``, the bounds the scenario's own
    ``$include`` was loaded with; ``validate --deep`` passes its own.
    """

    root: Path | None = None
    max_parent_traversal: int | None = None


UNBOUNDED = ReferenceBounds()


class BodySchema:
    """A body schema ready to validate a response body: the schema, the
    dialect it is validated under, and the registry its ``$ref``s resolve
    through. ``where`` names it for a failure (``body schema file
    'api/openapi.json#/components/schemas/User'``).

    The schema is resolved as JSON Schema resolves the schema it is given
    (`_prepared`): the ``$id`` of its document's root, if any, is the base
    its references resolve against, else where the document is, the file or
    the scenario's directory; the pointer is followed from there, each
    ``$id`` on the way moving the base. Where the way leaves JSON Schema's
    keywords (an OpenAPI ``components/schemas`` map, which JSON Schema itself
    does not look into), the outermost schema the selected one is in through
    them is a schema all the same, a component, whose own ``$id`` moves the
    base too. A schema taken out of a document this way keeps the document
    around it, so ``#/components/...`` inside it still names the
    document's. A document a reference reaches is resolved as jsonschema
    resolves one: by its location, whatever ``$id`` its root declares.

    Built once per verify step, from what is kept across them: the documents
    it reads are parsed once for as long as their file is unchanged
    (`read_schema_document`), and a schema file's registry is crawled once
    too (`_prepared_file`), an inline schema's for as long as it is the same
    object (one without templates is). Only the retrieval of other files is
    the step's own (`_LocalFiles`).
    """

    __slots__ = ("_bounds", "_check", "_dialect", "_files", "_prepare", "_rendered", "where")

    def __init__(
        self,
        where: str,
        prepare: Callable[[], "_Prepared"],
        files: "_LocalFiles",
        dialect: Dialect,
        check: Callable[[], None],
        bounds: ReferenceBounds,
        rendered: Any = None,
    ) -> None:
        self.where = where
        self._prepare = prepare
        self._files = files
        self._dialect = dialect
        self._check = check
        self._bounds = bounds
        # An inline schema, whose templates are rendered before it is used.
        self._rendered = rendered

    def check(self) -> None:
        """Check the schema against its dialect's meta-schema, raising what
        the meta-check raises: a SchemaError, or a crash (``re`` refusing a
        ``pattern``, the meta-validator's own recursion on a deep schema)."""
        self._check()

    def validate(self, instance: Any) -> None:
        """Validate ``instance``, raising what jsonschema raises: a
        ValidationError, a RecursionError for one nested too deeply, a
        ``referencing`` Unresolvable for a reference that does not resolve or
        an ``$id`` in the schema, or on the way to it, that cannot be read
        (`why_unresolvable` says why), an `InvalidReferencedSchema` for a
        schema a reference reaches that is not valid in its dialect, found
        when validating against it failed, or what a keyword raises on a
        value a referenced document gives it that its meta-schema allows."""
        schema, resolver = self._start()
        _validator(self._dialect, schema, resolver, self._bounds.max_parent_traversal).validate(instance)

    def why_unresolvable(self, error: referencing.exceptions.Unresolvable) -> tuple[str, bool]:
        """Why a reference did not resolve, and whether it named a local file
        that does not exist (``validate --deep`` reports that one as missing).

        In words that name what it looked for, not the document it looked in:
        referencing's own message for a pointer to nothing quotes that whole
        document, an OpenAPI document included. The reference, as written,
        and its keyword are the plugin's own Unresolvable's (`_resolve`), or
        what referencing's names for a reference whose file could not be
        retrieved.
        """
        # What was raised while handling what: explicitly chained by the
        # versions that do, implicitly by the older ones (jsonschema 4.18,
        # referencing before 0.33). By identity: jsonschema's wrapper compares
        # equal to what it wraps.
        chain: list[BaseException] = []
        cause: BaseException | None = error
        while cause is not None and not any(cause is seen for seen in chain):
            chain.append(cause)
            cause = cause.__cause__ if cause.__cause__ is not None or cause.__suppress_context__ else cause.__context__
        # The reference as written, which may be any JSON value (`null`).
        written: Any = _UNKNOWN
        keyword = "$ref"
        for cause in chain:
            # Exactly these: referencing's subclasses carry what they looked
            # for as `ref`, a pointer, an anchor or a resolved URI.
            if written is _UNKNOWN and isinstance(cause, referencing.exceptions.Unresolvable) and type(cause) in _NAMED_BY:
                written = cause.ref
                keyword = _NAMED_BY[type(cause)] or keyword
            subject = f"{keyword} {_quoted(written)}" if written is not _UNKNOWN else f"a {keyword}"
            # A reference that is its fragment alone shows it already.
            bare = isinstance(written, str) and written.startswith("#")
            match cause:
                case _Refused(missing=missing):
                    return f"{subject} {cause}", missing
                case _Malformed():
                    return f"{subject} {cause}", False
                case referencing.exceptions.PointerToNowhere(ref=pointer, resource=resource):
                    where = "" if bare else f": '#{pointer}' is not"
                    return f"{subject} points to nothing{where} in {self._files.name(resource)}", False
                case referencing.exceptions.NoSuchAnchor(anchor=anchor, resource=resource):
                    return f"{subject} names the anchor {anchor!r}, which is not in {self._files.name(resource)}", False
                case referencing.exceptions.InvalidAnchor(anchor=anchor):
                    what = subject if bare else f"{subject}: '#{anchor}'"
                    return f"{what} is neither a JSON pointer (one starts with '/') nor an anchor", False
                case ValueError() | TypeError() | AttributeError():
                    # A pointer that cannot go on (`_resolve`): referencing
                    # reads an index into an array with int() and indexes
                    # whatever it reached; urljoin refuses a malformed URI.
                    return f"{subject} cannot be resolved: {cause}", False
        subject = f"{keyword} {_quoted(written)}" if written is not _UNKNOWN else f"a {keyword}"
        return f"{subject} cannot be resolved", False

    def unresolvable(self) -> Iterator[tuple[str, bool]]:
        """Each reference the schema reaches that does not resolve, or whose
        target is not a valid schema of its dialect, as `why_unresolvable`
        has it, for ``validate --deep``: what the runtime would find on some
        body, found without one.

        Walked as jsonschema descends: into each subschema, whose ``$id``
        moves the base URI, and through each ``$ref`` and ``$dynamicRef`` to
        its target, the base then being the target's document, the dialect
        the one `_reached_dialect` gives it. Under Draft 3 to 7, a ``$ref``
        stands alone, as they validate it (`_ref_alone`): the keywords beside
        it, references included, are not walked. Each schema once, each
        problem once, however many references share it. A template in an
        inline schema is rendered before the schema is used, so a reference
        (or an ``$id``) there that holds one, or an escaped ``\\{{``, is known
        only then; a file, and any document a reference reaches, is read as
        it is, templates and all.
        """
        reported: set[str] = set()
        try:
            schema, resolver = self._start()
        except referencing.exceptions.Unresolvable as e:
            # An $id that cannot be read, at the root, on the pointer's way,
            # or in the schema.
            yield self.why_unresolvable(e)
            return
        rendered = _dict_ids(self._rendered)
        # (a schema, the resolver its references resolve with, the dialect it is
        # reached under, and whether it is a subschema, whose own $id moves the
        # base, or the start or a reference's target, whose base is set):
        # referencing's Resolver is not public.
        pending: list[tuple[Any, Any, Dialect, bool]] = [(schema, resolver, self._dialect, False)]
        seen = {id(schema)}
        while pending:
            contents, resolver, dialect, nested = pending.pop()
            if not isinstance(contents, dict):
                continue
            templated = id(contents) in rendered
            schema_id = contents.get("$id")
            if templated and isinstance(schema_id, str) and needs_rendering(schema_id):
                continue
            dialect = _declared_dialect(contents, dialect)
            specification = _specification(dialect)
            resource = specification.create_resource(contents)
            problems: list[tuple[str, bool]] = []
            if nested:
                try:
                    # An $id that cannot be read, named as the runtime names
                    # one; else where jsonschema moves the base to.
                    _moved(_base_uri(resolver), contents, specification)
                    resolver = resolver.in_subresource(resource)
                except referencing.exceptions.Unresolvable as e:
                    problems.append(self.why_unresolvable(e))
                    resource = None
            alone = dialect in _REF_ALONE_DIALECTS and contents.get("$ref") is not None
            if resource is not None:
                for keyword in _REFERENCE_KEYWORDS:
                    if keyword not in dialect.VALIDATORS or keyword not in contents:
                        continue
                    ref = contents[keyword]
                    if templated and isinstance(ref, str) and needs_rendering(ref):
                        continue
                    problem = self._follow(keyword, ref, resolver, dialect, seen, pending)
                    if problem is not None:
                        problems.append(problem)
            for problem in problems:
                if problem[0] not in reported:
                    reported.add(problem[0])
                    yield problem
            if resource is None or alone:
                continue
            # Reversed onto the stack, so siblings are taken in document order.
            for subresource in reversed(list(resource.subresources())):
                if id(subresource.contents) not in seen:
                    seen.add(id(subresource.contents))
                    pending.append((subresource.contents, resolver, dialect, True))

    def _follow(self, keyword: str, ref: Any, resolver: Any, dialect: Dialect, seen: set[int], pending: list[tuple[Any, Any, Dialect, bool]]) -> tuple[str, bool] | None:
        """`unresolvable`'s step through one reference: why it does not
        resolve, or why its target is not a valid schema, or None, the target
        then queued to walk if it is new."""
        try:
            resolved, reached = _resolve(keyword, ref, resolver, dialect, self._bounds.max_parent_traversal)
        except referencing.exceptions.Unresolvable as e:
            return self.why_unresolvable(e)
        if id(resolved.contents) in seen:
            return None
        seen.add(id(resolved.contents))
        try:
            reached.check_schema(resolved.contents)
        except Exception as e:
            return str(_invalid_target(keyword, ref, e)), False
        pending.append((resolved.contents, resolved.resolver, reached, False))
        return None

    def _start(self) -> tuple[Any, Any]:
        """The schema, and the resolver its references resolve with: on the
        registry `_prepared` keeps, which retrieves other files for this step
        alone (`_LocalFiles`), at the base it found. An Unresolvable naming
        an ``$id`` that cannot be read, where it found one."""
        schema, registry, base = self._prepare()
        return schema, registry.combine(referencing.Registry(retrieve=self._files)).resolver(base_uri=base)


# A reference not known yet, in `BodySchema.why_unresolvable`: a written one
# may be any JSON value, null included.
_UNKNOWN = object()


class InvalidReferencedSchema(Exception):
    """A schema a ``$ref`` or ``$dynamicRef`` reaches that is not valid in its
    dialect, named by the reference, in the words ``validate --deep`` uses.
    Found at runtime when validating against it failed (`_reference`): only
    the selected schema is meta-checked before."""


@dataclass(frozen=True, slots=True)
class SchemaFile:
    """A ``body.schema`` file reference, located: the file, its path resolved
    against the scenario's directory as every file path in a scenario is, and
    the JSON pointer into it, as written (``fragment``) and as segments.
    Shown as the path, then ``#`` and the pointer as written, if any."""

    path: Path
    fragment: str
    pointer: tuple[str, ...]

    @classmethod
    def locate(cls, ref: str, scenario_dir: Path | None) -> "SchemaFile":
        parsed = parse_schema_file_ref(ref)
        return cls(resolve_scenario_path(scenario_dir, parsed.path), parsed.fragment, parsed.pointer)

    def __str__(self) -> str:
        return _shown(f"{self.path}#{self.fragment}" if self.fragment else str(self.path))


def file_body_schema(file: SchemaFile, bounds: ReferenceBounds = UNBOUNDED) -> BodySchema:
    """The schema a ``body.schema`` file reference selects.

    Raises `SchemaFileError` for a file that cannot be read as JSON and
    `SchemaPointerError` for a pointer that leads nowhere in it. The schema is
    not meta-checked here (`BodySchema.check`).
    """
    stamp, document = _read(file.path)
    schema = follow_pointer(document, file.pointer)
    dialect = schema_dialect(schema, document)
    files = _LocalFiles(bounds.root, _specification(dialect))
    files.names[id(document)] = str(file.path)
    path = Path(os.path.abspath(file.path))
    return BodySchema(
        f"body schema file '{file}'",
        functools.partial(_prepared_file, path, stamp, file.pointer, dialect),
        files,
        dialect,
        functools.partial(_meta_check, path, stamp, file.pointer),
        bounds,
    )


def inline_body_schema(schema: dict[str, Any], scenario_dir: Path | None, bounds: ReferenceBounds = UNBOUNDED) -> BodySchema:
    """An inline ``body.schema`` (the model has meta-checked it), whose
    references to files resolve against the scenario's directory, as a schema
    file's path does, or the working directory without one."""
    directory = Path(os.path.abspath(scenario_dir if scenario_dir is not None else Path.cwd()))
    # A directory's URI ends in '/', so a relative reference resolves inside it.
    uri = directory.as_uri().rstrip("/") + "/"
    dialect = json_schema_validator_class(schema)
    files = _LocalFiles(bounds.root, _specification(dialect))
    files.names[id(schema)] = "the inline schema"
    return BodySchema(
        "inline body schema",
        functools.partial(_prepared_inline, _Same(schema), uri, dialect),
        files,
        dialect,
        functools.partial(dialect.check_schema, schema),
        bounds,
        rendered=schema,
    )


def schema_dialect(schema: Any, document: Any) -> Dialect:
    """The dialect ``schema``, taken out of ``document`` by a pointer, is
    validated under: its own ``$schema``, else the document root's, else
    Draft 2020-12. A ``$schema`` jsonschema does not know (OpenAPI 3.1's
    ``https://spec.openapis.org/oas/3.1/dialect/base``) is Draft 2020-12.
    What a ``$ref`` reaches follows the same rule (`_reached_dialect`)."""
    for holder in (schema, document):
        if isinstance(holder, dict) and isinstance(holder.get("$schema"), str):
            return json_schema_validator_class(holder)
    return DEFAULT_DIALECT


def follow_pointer(document: Any, pointer: tuple[str, ...]) -> Any:
    """What a JSON pointer's segments select in ``document``, or a
    `SchemaPointerError` saying where the pointer left it. An array index is
    ``0`` or digits without a leading zero, as RFC 6901 has it."""
    node = document
    for depth, segment in enumerate(pointer):
        if isinstance(node, dict) and segment in node:
            node = node[segment]
            continue
        # An index with more digits than the length cannot be in range, and
        # int() refuses more than some thousands of them.
        if isinstance(node, list) and _ARRAY_INDEX.fullmatch(segment) and len(segment) <= len(str(len(node))) and int(segment) < len(node):
            node = node[int(segment)]
            continue
        at = "'#" + "".join(f"/{_escape(passed)}" for passed in pointer[:depth]) + "'"
        match node:
            case dict():
                raise SchemaPointerError(f"{at} has no key {segment!r}")
            case list():
                raise SchemaPointerError(f"{at} is an array of {len(node)}, with no item {segment!r}")
            case _:
                raise SchemaPointerError(f"{at} is {_json_kind(node)}, with nothing in it to select")
    return node


def read_schema_document(path: Path) -> Any:
    """The JSON a schema file holds, or a `SchemaFileError` (`read_json_schema_file`)."""
    return _read(path)[1]


class _Once:
    """A function's results kept by its arguments, the ``maxsize`` most
    recently used, as ``functools.lru_cache`` keeps them, but each computed
    once, however many threads ask for it at the same time: a parallel
    stage's iterations all start by asking for the same schema file, whose
    parse, meta-check and crawl take seconds for a large one, and with
    lru_cache each thread computed them for itself, the GIL shared among
    them. A failure is not kept: the next call computes again."""

    __slots__ = ("__wrapped__", "_entries", "_lock", "_maxsize")

    def __init__(self, function: Callable[..., Any], maxsize: int) -> None:
        self.__wrapped__ = function
        self._maxsize = maxsize
        self._lock = threading.Lock()
        self._entries: collections.OrderedDict[tuple[Any, ...], _Entry] = collections.OrderedDict()

    def __call__(self, *args: Any) -> Any:
        with self._lock:
            entry = self._entries.get(args)
            if entry is None:
                entry = self._entries[args] = _Entry()
                if len(self._entries) > self._maxsize:
                    self._entries.popitem(last=False)
            else:
                self._entries.move_to_end(args)
        with entry.lock:
            if not entry.done:
                try:
                    entry.value = self.__wrapped__(*args)
                except BaseException:
                    with self._lock:
                        if self._entries.get(args) is entry:
                            del self._entries[args]
                    raise
                entry.done = True
            return entry.value

    def cache_clear(self) -> None:
        with self._lock:
            self._entries.clear()


class _Entry:
    __slots__ = ("done", "lock", "value")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.done = False
        self.value: Any = None


def _once(maxsize: int) -> Callable[[Callable[..., Any]], _Once]:
    return lambda function: _Once(function, maxsize)


def _read(path: Path) -> tuple[tuple[int, int], Any]:
    """A schema file's modification time and size, and its JSON, parsed once
    for as long as those are unchanged: a verify step runs on every iteration
    of its stage, a parallel one's included, and an OpenAPI document can be
    large. A file that changes (a stage writes it) is read again."""
    try:
        stat = path.stat()
    except (OSError, ValueError) as e:
        # ValueError: a path the OS cannot take, a NUL or a lone surrogate
        # one JSON \u escape or a rendered template away.
        raise SchemaFileError(str(e)) from e
    stamp = (stat.st_mtime_ns, stat.st_size)
    return stamp, _parsed(Path(os.path.abspath(path)), stamp)


@_once(maxsize=64)
def _parsed(path: Path, stamp: tuple[int, int]) -> Any:
    """`read_json_schema_file`, kept by path and `_read`'s stamp. Nothing the
    documents go to (the pointer, the registry, jsonschema) changes them, so
    one parse is shared by every step, thread and scenario that reads the
    file. A file that fails to read is not kept."""
    return read_json_schema_file(path)


# What `BodySchema._start` builds on: the selected schema, the registry holding
# its document (the meta-schemas too, as jsonschema adds them to the registry
# it is given, for a `$schema` or a `$ref` naming one), and the base URI its
# references resolve against there.
type _Prepared = tuple[Any, referencing.Registry[Any], str]


@_once(maxsize=64)
def _prepared_file(path: Path, stamp: tuple[int, int], pointer: tuple[str, ...], dialect: Dialect) -> _Prepared:
    """`_prepared` for a schema file, kept by path, `_read`'s stamp and the
    pointer, as the parse is: a registry is immutable, so one crawl of an
    OpenAPI document or a file of definitions serves every step, thread and
    scenario that validates against it."""
    document = _parsed(path, stamp)
    return _prepared(document, _file_uri(path), pointer, _specification_of(document, _specification(dialect)))


@_once(maxsize=64)
def _prepared_inline(schema: "_Same", uri: str, dialect: Dialect) -> _Prepared:
    """`_prepared` for an inline schema, kept for as long as it is the same
    object: every iteration's, when it holds no template (a rendered one is
    a new object each time)."""
    return _prepared(schema.value, uri, (), _specification_of(schema.value, _specification(dialect)))


def _prepared(document: Any, uri: str, pointer: tuple[str, ...], specification: referencing.Specification[Any]) -> _Prepared:
    """The schema ``pointer`` selects in ``document`` (found at ``uri``), the
    registry, crawled, and the base URI of the schema's references.

    Resolved as JSON Schema resolves the schema it is given: the base is the
    document root's ``$id`` (else ``uri``), then the pointer is followed as
    referencing follows one, an ``$id`` moving the base where JSON Schema
    reads a schema (in ``$defs``, ``properties``...). Where it does not, as
    in an OpenAPI ``components/schemas`` map, the walk is JSON Schema's again
    from the outermost schema the selected one is in through JSON Schema's
    keywords alone (`_component_depth`), the component: its own ``$id``
    moves the base all the same, and it is added to the registry with the
    resources in it, so a reference by one of their ``$id``s finds them, as
    the registry's crawl finds those JSON Schema looks for.

    An ``$id`` that cannot be read raises an Unresolvable naming it
    (`_moved`): on the pointer's way, and, where the crawl fails on one, in
    the selected schema (`_id_problem`). One elsewhere in the document is
    left to fail where jsonschema meets it, as it would.

    A crawl that fails (on such an ``$id``, or on what is no schema where one
    belongs, a Draft 7 array ``items`` under 2020-12) indexes nothing, so the
    document, and a component, is kept at its own ``$id`` all the same
    (`_kept_at`), as jsonschema keeps the schema it is given: a reference
    that needs no crawl, ``#/$defs/...`` under that ``$id``, resolves, and one
    that does fails on what the crawl fails on, not as a remote document."""
    resource = specification.create_resource(document)
    registry = _META_SCHEMAS.combine(referencing.Registry().with_resource(uri, resource))
    try:
        # Once, for every lookup from here: referencing crawls the registry
        # a lookup misses.
        registry = registry.crawl()
    except Exception:
        crawled = False
    else:
        crawled = True
    base = _moved(uri, document, specification)
    if not crawled:
        registry = _kept_at(registry, base, resource)
    nodes = [document]
    keys: list[int | str] = []
    for segment in pointer:
        # As `follow_pointer` checked it: an index into an array, else a key.
        key = int(segment) if isinstance(nodes[-1], list) else segment
        nodes.append(nodes[-1][key])
        keys.append(key)
    start = _component_depth(nodes, keys, specification)
    if start:
        # The component: as JSON Schema reads the document, the way to it
        # moves the base, but only as far as it reads schemas on it.
        base = _descend(base, nodes[: start + 1], keys[:start], specification)
        component = nodes[start]
        specification = _specification_of(component, specification)
        inside = _moved(base, component, specification)
        resource = specification.create_resource(component)
        try:
            # The registry's own entry wins where both have one: the document
            # the component is in, at the base it had there.
            registry = referencing.Registry().with_resource(base, resource).crawl().combine(registry)
        except Exception:
            crawled = False
            if inside != base:
                registry = _kept_at(registry, inside, resource)
        else:
            # The crawl that reaches the selected schema.
            crawled = True
        base = inside
    base = _descend(base, nodes[start:], keys[start:], specification)
    schema = nodes[-1]
    if not crawled and (problem := _id_problem(schema, base, specification)) is not None:
        raise problem
    return schema, registry, base


def _kept_at(registry: referencing.Registry[Any], uri: str, resource: referencing.Resource[Any]) -> referencing.Registry[Any]:
    """``registry`` with ``resource``, whose crawl failed, kept at ``uri``,
    its own ``$id`` (`_prepared`). Uncrawled, as jsonschema keeps the schema
    it is given: a lookup the registry misses crawls it, and fails where its
    crawl failed. Where ``uri`` is the one it has already, it is left."""
    if uri in registry:
        return registry
    return registry.with_resource(uri, resource)


def _component_depth(nodes: list[Any], keys: list[int | str], specification: referencing.Specification[Any]) -> int:
    """How deep in the pointer's way (``nodes``, through ``keys``) the
    component is (`_prepared`): the outermost node from which the walk to the
    selected schema is JSON Schema's keywords alone (`_enters`); 0 where the
    document is such a schema.

    Unless that walk reads a deeper such node as what a keyword holds, a map
    or array of schemas (`_holds_schemas`), which that one cannot be: then
    the deeper one is. A component named like a keyword that holds a map
    (``properties``, ``$defs``, ``patternProperties``, ``definitions``) is
    under ``components/schemas`` as a property is under ``properties``: read
    from that map, the component was the map of properties, its ``$id`` a
    property's schema, never its base."""
    candidates = [depth for depth in range(len(keys) + 1) if _enters(nodes[depth:], keys[depth:], specification)]
    start = candidates[0]
    for later in candidates[1:]:
        if not _enters(nodes[start : later + 1], keys[start:later], specification) and not _holds_schemas(nodes[later]):
            start = later
    return start


def _holds_schemas(node: Any) -> bool:
    """Whether ``node`` may be what a keyword holding schemas holds: a map or
    an array of schemas (objects and booleans) or, as Draft 7's
    ``dependencies`` may, of arrays of names. A schema itself holds text
    (``type``, ``$id``, ``title``) or a number beside its subschemas."""
    members = node.values() if isinstance(node, dict) else node if isinstance(node, list) else None
    return members is not None and all(isinstance(member, dict | bool | list) for member in members)


def _enters(nodes: list[Any], keys: list[int | str], specification: referencing.Specification[Any]) -> bool:
    """Whether referencing's pointer walk from ``nodes[0]`` through ``keys``
    (to ``nodes[1:]``) ends in a schema, one ``maybe_in_subresource``
    enters: through JSON Schema's keywords alone. Where the walk passes an
    ``$id``, it starts over from there, as referencing's does."""
    segments: list[int | str] = []
    entered = True
    for key, node in zip(keys, nodes[1:], strict=True):
        segments.append(key)
        entered = _entered(segments, node, specification)
        if entered and _declares_id(node, specification):
            segments = []
    return entered


def _descend(base: str, nodes: list[Any], keys: list[int | str], specification: referencing.Specification[Any]) -> str:
    """The base URI at the end of `_enters`'s walk, started at ``base``: each
    schema it enters that declares an ``$id`` moves it (`_moved`)."""
    segments: list[int | str] = []
    for key, node in zip(keys, nodes[1:], strict=True):
        segments.append(key)
        if _entered(segments, node, specification) and _schema_id(node, specification) is not None:
            base = _moved(base, node, specification)
            segments = []
    return base


def _entered(segments: list[int | str], node: Any, specification: referencing.Specification[Any]) -> bool:
    """Whether ``specification`` reads ``node``, reached through ``segments``
    from a resource, as a schema of its own (``maybe_in_subresource``)."""
    probe = _Probe()
    # A stand-in for a resolver: `in_subresource` is all it is asked for.
    specification.maybe_in_subresource(segments=segments, resolver=cast(Any, probe), subresource=specification.create_resource(node))
    return probe.entered


def _declares_id(node: Any, specification: referencing.Specification[Any]) -> bool:
    """Whether ``node`` declares an ``$id``, one that cannot be read too:
    `_enters` looks for where the walk starts over, not at the base."""
    try:
        return _schema_id(node, specification) is not None
    except referencing.exceptions.Unresolvable:
        return True


def _id_problem(schema: Any, base: str, specification: referencing.Specification[Any]) -> referencing.exceptions.Unresolvable | None:
    """The first ``$id`` in a subschema of ``schema`` that cannot be read
    (`_moved`), found as referencing's crawl walks it, or None. ``base`` is
    the one inside ``schema``: its own ``$id`` is read where it is reached.

    jsonschema meets such an ``$id`` as it descends into its subschema and
    fails there with urljoin's ValueError, which says nothing of an ``$id``.
    So it is named as ``validate --deep`` names it: in the selected schema at
    the start, where its document's crawl failed (`_prepared`), and in a
    reference's target once validating against it broke down
    (`_raise_if_invalid`). A keyword whose value is not what the meta-schema
    wants is the meta-check's to refuse, and is passed over."""
    pending = [(schema, base, specification, False)]
    while pending:
        contents, base, specification, nested = pending.pop()
        if not isinstance(contents, dict):
            continue
        try:
            if nested:
                base = _moved(base, contents, specification)
            subresources = list(specification.subresources_of(contents))
        except referencing.exceptions.Unresolvable as e:
            return e
        except Exception:
            continue
        pending.extend((each, base, _specification_of(each, specification), True) for each in reversed(subresources))
    return None


@_once(maxsize=256)
def _meta_check(path: Path, stamp: tuple[int, int], pointer: tuple[str, ...]) -> None:
    """Meta-check the schema a pointer selects in a file, in its dialect,
    once for as long as the file is unchanged (a failure is checked again)."""
    document = _parsed(path, stamp)
    schema = follow_pointer(document, pointer)
    schema_dialect(schema, document).check_schema(schema)


@functools.cache
def format_checker(dialect: Dialect) -> jsonschema.FormatChecker:
    """The dialect's own format checker, so the checked formats are the ones
    the schema's ``$schema`` defines and the installed jsonschema can check,
    except that a check which crashes counts as a nonconforming value.

    jsonschema turns only the exceptions a checker declares into a format
    failure, and response data reaches the others: ``regex`` compiles the
    value with ``re``, which raises OverflowError on ``a{4294967296}`` and
    RecursionError on deeply nested groups. Those would escape as a raw
    traceback past the abort machinery, with no request/response report.
    A value its format's checker cannot process does not conform to it.
    """
    checker = jsonschema.FormatChecker(formats=())
    for name, (func, _declared) in dialect.FORMAT_CHECKER.checkers.items():
        checker.checks(name, raises=Exception)(func)
    return checker


def _validator(dialect: Dialect, schema: Any, resolver: Any, max_parent_traversal: int | None) -> Any:
    """A validator for ``schema``, reached with ``resolver``, in ``dialect``,
    its references followed by `_reference`."""
    return _referencing(dialect, max_parent_traversal)(schema, _resolver=resolver, format_checker=format_checker(dialect))


@functools.cache
def _referencing(dialect: Dialect, max_parent_traversal: int | None) -> Any:
    """``dialect``, its ``$ref`` and ``$dynamicRef`` followed by
    `_reference` rather than by jsonschema itself, for every subschema below:
    jsonschema keeps a validator's class for a subschema that declares no
    ``$schema`` of its own, and this ``evolve`` gives one that declares one
    (an embedded resource, as a bundled document has them) this class's
    counterpart in that dialect, where jsonschema's gives its own class.

    Built with ``create``, as jsonschema builds the dialect, not with
    ``extend``: jsonschema 4.18's ``extend`` drops the dialect's rule for
    which keywords apply, so under Draft 3 to 7, which ignore a ``$ref``'s
    siblings, they would be validated.

    One lookup stays jsonschema's: ``unevaluatedProperties`` and
    ``unevaluatedItems`` look a reference up themselves, to see what it
    evaluated. It goes through the same registry, local and inside the root,
    but without the written-path rules. So those two apply last in a schema
    (`_unevaluated_last`), as the annotations they read come from the others:
    each reference they look up is one `_reference` has followed by then,
    held to the rules, in the schema they are in, in what that reaches, and
    in the ``allOf``/``anyOf``/``oneOf``, ``if``/``then``/``else`` and
    ``dependentSchemas`` subschemas they look into. Applied in the schema's
    order, they had the file of a rule-breaking ``$ref`` written after them
    read, which could make a body pass under ``not``; written before them,
    the ``$ref`` failed the stage."""
    keywords = {keyword: functools.partial(_reference, keyword, dialect, max_parent_traversal) for keyword in _REFERENCE_KEYWORDS if keyword in dialect.VALIDATORS}
    validators: dict[str, Any] = {**dialect.VALIDATORS, **keywords}
    extended: Any = jsonschema.validators.create(
        meta_schema=dialect.META_SCHEMA,
        validators=validators,
        type_checker=dialect.TYPE_CHECKER,
        format_checker=dialect.FORMAT_CHECKER,
        id_of=dialect.ID_OF,
        applicable_validators=_ref_alone if dialect in _REF_ALONE_DIALECTS else _unevaluated_last,
    )

    def evolve(self: Any, **changes: Any) -> Any:
        # jsonschema's own evolve, but for the class it picks.
        schema = changes.setdefault("schema", self.schema)
        declared = _declared_dialect(schema, dialect)
        if declared is not dialect:
            changes.setdefault("format_checker", format_checker(declared))
        for field in attrs.fields(type(self)):
            if field.init and field.alias not in changes:
                changes[field.alias] = getattr(self, field.name)
        return _referencing(declared, max_parent_traversal)(**changes)

    extended.evolve = evolve
    return extended


# The dialects in which a `$ref` stands alone, its sibling keywords ignored,
# as they specify and as jsonschema validates them (its `ignore_ref_siblings`).
_REF_ALONE_DIALECTS = frozenset({jsonschema.Draft3Validator, jsonschema.Draft4Validator, jsonschema.Draft6Validator, jsonschema.Draft7Validator})


# The keywords whose jsonschema implementation looks references up itself
# (`_referencing`), which apply last (`_unevaluated_last`).
_UNEVALUATED = ("unevaluatedProperties", "unevaluatedItems")


def _unevaluated_last(schema: Any) -> Any:
    """The keywords that apply in a schema of a later dialect: all, in the
    schema's order, but ``unevaluatedProperties`` and ``unevaluatedItems``
    after the others (`_referencing`)."""
    if not any(keyword in schema for keyword in _UNEVALUATED):
        return schema.items()
    return sorted(schema.items(), key=lambda item: item[0] in _UNEVALUATED)


def _ref_alone(schema: Any) -> Any:
    """The keywords that apply in a schema of a `_REF_ALONE_DIALECTS` dialect:
    its ``$ref`` alone, if it has one."""
    ref = schema.get("$ref")
    return [("$ref", ref)] if ref is not None else schema.items()


def _reference(keyword: str, dialect: Dialect, max_parent_traversal: int | None, validator: Any, ref: Any, instance: Any, schema: Any) -> Iterator[Any]:
    """A ``$ref`` or ``$dynamicRef``: what jsonschema does for one (look the
    reference up with the validator's resolver, validate the instance against
    its target), and three things it does not.

    A reference to a file is held to the path rules a scenario's
    ``$include`` keeps (`_file_refusal`), as written: by the time the
    registry asks for a file, it has the URI the reference resolved to. The
    target is validated under the dialect `_reached_dialect` gives it, where
    jsonschema looks at the target's own ``$schema`` alone, so the schema a
    pointer selects in a Draft 7 document is Draft 7 however it is reached.
    And a target that validating against crashes is meta-checked then: only
    the selected schema is before, and a keyword crashes on a value its
    meta-schema refuses (``"type": "strin"``, ``"$ref": 5``, a document that
    is a list), so the failure is said as ``validate --deep`` says it, an
    `InvalidReferencedSchema` naming the reference.

    Looked up once per resolver and reference (`_target`): an array's items
    are each validated with the same resolver, by a reference to the same
    schema.
    """
    # The resolver jsonschema validates with (a private attribute, as in its
    # own `$ref`): the base URI and the registry the reference resolves in.
    resolver = validator._resolver
    if not isinstance(ref, str):
        _not_a_string(keyword, ref)
    reached, target = _target(_Same(resolver), keyword, ref, dialect, max_parent_traversal)
    try:
        yield from target.iter_errors(instance)
    except (InvalidReferencedSchema, RecursionError):
        # Named already, by the reference nearest to it; or too deep for a
        # meta-check to tell anything here.
        raise
    except Exception:
        _raise_if_invalid(keyword, ref, reached, target.schema, target._resolver)
        raise


@functools.lru_cache(maxsize=1024)
def _target(resolver: "_Same", keyword: str, ref: str, dialect: Dialect, max_parent_traversal: int | None) -> tuple[Dialect, Any]:
    """The dialect of what a reference names, and a validator for it
    (`_resolve`), kept by the resolver it is looked up with, which holds the
    base URI, the registry and the dynamic scope a lookup depends on. A
    resolver is immutable, and each verify step has its own (`BodySchema`),
    so what is kept for one step serves no other."""
    resolved, reached = _resolve(keyword, ref, resolver.value, dialect, max_parent_traversal)
    try:
        # A newer jsonschema reads the schema's keywords as it builds one.
        target = _validator(reached, resolved.contents, resolved.resolver, max_parent_traversal)
    except Exception:
        _raise_if_invalid(keyword, ref, reached, resolved.contents, resolved.resolver)
        raise
    return reached, target


def _resolve(keyword: str, ref: Any, resolver: Any, dialect: Dialect, max_parent_traversal: int | None) -> tuple[Any, Dialect]:
    """What a reference names, looked up as referencing looks it up (a
    Resolved), and the dialect it is validated under (`_reached_dialect`),
    for the runtime and ``validate --deep`` alike.

    Or an Unresolvable naming the reference as written, with its keyword
    (`BodySchema.why_unresolvable`), from why: it is not a string, it breaks
    the path rules (`_file_refusal`), or its lookup failed, with
    referencing's own Unresolvable or with what referencing lets through (a
    pointer that cannot go on: an array indexed by a name, which int()
    refuses; a string or a number indexed at all; a URI urljoin refuses)."""
    if not isinstance(ref, str):
        _not_a_string(keyword, ref)
    refused = _file_refusal(ref, resolver, max_parent_traversal)
    if refused is not None:
        raise _unresolvable(keyword, ref) from refused
    try:
        resolved, root = _lookup(resolver, ref)
    except RecursionError:
        # Out of stack in the middle of a validation that recurses as deep as
        # the body (``"items": {"$ref": "#"}``), wherever the lookup it was
        # making: the body's failure, not a reference that does not resolve.
        raise
    except Exception as e:
        raise _unresolvable(keyword, ref) from e
    return resolved, _reached_dialect(resolved.contents, root, dialect)


def _not_a_string(keyword: str, ref: Any) -> NoReturn:
    raise _unresolvable(keyword, ref) from _Malformed("is not a string: a reference is a URI reference")


def _raise_if_invalid(keyword: str, ref: str, dialect: Dialect, schema: Any, resolver: Any) -> None:
    """Why validating against a reference's target broke down, as
    ``validate --deep`` finds it: an `InvalidReferencedSchema` for one that
    fails its dialect's meta-check, a crash of the check included (as
    `BodySchema.check` and deep count one), or the Unresolvable naming an
    ``$id`` in it urljoin cannot join, which jsonschema met as it descended
    (`_id_problem`), from ``resolver``'s base. Else nothing: neither, or the
    meta-check ran out of stack, which a deep validation leaves it little of."""
    try:
        dialect.check_schema(schema)
    except RecursionError:
        return
    except Exception as e:
        raise _invalid_target(keyword, ref, e) from e
    problem = _id_problem(schema, _base_uri(resolver), _specification(dialect))
    if problem is not None:
        raise problem


def _invalid_target(keyword: str, ref: str, error: Exception) -> InvalidReferencedSchema:
    return InvalidReferencedSchema(f"{keyword} {ref!r} points to an invalid JSON Schema: {schema_error_text(error)}")


def _lookup(resolver: Any, ref: str) -> tuple[Any, Any]:
    """``ref`` looked up as referencing looks it up (a Resolved), and the
    root of the document it points into, for a pointer or an anchor after
    the ``#`` (else None: what it reaches is that root). The document the
    reference's URI names, or the one it is in for a bare ``#...``, then the
    fragment in it: the same base URI, registry and dynamic scope as one
    lookup of the whole reference, and one that misses the registry, for a
    reference to another file, not two."""
    document, _, fragment = ref.partition("#")
    if not fragment:
        return resolver.lookup(ref), None
    root = resolver.lookup(document)
    return root.resolver.lookup("#" + fragment), root.contents


def _reached_dialect(contents: Any, root: Any, referrer: Dialect) -> Dialect:
    """The dialect a reference's target is validated under: its own
    ``$schema``; else the root ``$schema`` of the document it is in
    (`_lookup`), as for the schema a ``body.schema`` pointer selects
    (`schema_dialect`); else the dialect of the schema the reference is in,
    ``referrer``, as jsonschema has it."""
    for holder in (contents, root):
        if _declares_dialect(holder):
            return jsonschema.validators.validator_for(holder, default=referrer)
    return referrer


def _declares_dialect(schema: Any) -> bool:
    return isinstance(schema, dict) and isinstance(schema.get("$schema"), str)


def _file_refusal(ref: str, resolver: Any, max_parent_traversal: int | None) -> "_Refused | None":
    """Why a reference to a file may not name it, as a scenario's ``$include``
    path may not (`jsonref.plumbing.path.validate_ref_path`), judged as
    written: it holds a NUL, it is a ``file:`` URI or an absolute path
    (``/...``, ``//host/...``, ``\\\\host\\...``, a drive), or it climbs through
    more ``..`` segments than ``max_parent_traversal``.

    A reference is to a file when it resolves, against the base it is
    written under (``resolver``'s), to a ``file:`` URI, or is a drive path.
    None for one that resolves to anything else, whatever its path looks
    like: under an ``$id`` of ``https://example.com/schemas/user``,
    ``/schemas/address`` names another ``$id``'s resource, or a remote
    document `_LocalFiles` refuses. None too for a relative path within the
    rules."""
    try:
        parts = urlsplit(ref)
        # A one-letter scheme is a Windows drive (C:/...): a path, whatever the base.
        if len(parts.scheme) != 1 and urlsplit(urljoin(_base_uri(resolver), ref)).scheme != "file":
            return None
    except ValueError:
        # Left to the lookup, which fails on it the same way.
        return None
    if "\0" in unquote(parts.path):
        return _Refused("contains a NUL character, which no file path can")
    relative = "a reference to a file is a path relative to the file it is in, as a scenario's $include path is"
    if parts.scheme == "file":
        return _Refused(f"is a file: URI, which is not allowed: {relative}")
    if len(parts.scheme) == 1 or ref.startswith(("/", "\\")):
        return _Refused(f"is an absolute path, which is not allowed: {relative}")
    climbs = re.split(r"[/\\]", unquote(parts.path)).count("..")
    if max_parent_traversal is not None and climbs > max_parent_traversal:
        return _Refused(f"exceeds the maximum parent traversal depth of {max_parent_traversal} (httpchain_ref_parent_traversal_depth), as a scenario's $include path would")
    return None


def _base_uri(resolver: Any) -> str:
    """The base URI ``resolver`` resolves a reference against. referencing
    keeps it private (``Resolver._base_uri``, the same from 0.28.4, the
    floor jsonschema 4.18.0 requires, on); test_body_schema pins it."""
    return resolver._base_uri


def _schema_id(schema: Any, specification: referencing.Specification[Any]) -> str | None:
    """The ``$id`` of ``schema`` (``id`` in Draft 3 and 4), as
    ``specification`` reads it, or None. An `_IdUnresolvable` for one that is
    not a string, where referencing's reading hands it back, or raises on it
    (Draft 3 to 7 read it with ``startswith``)."""
    if not isinstance(schema, dict):
        return None
    try:
        identifier = specification.id_of(schema)
    except (AttributeError, TypeError):
        identifier = schema.get(_id_keyword(specification))
    if identifier is not None and not isinstance(identifier, str):
        keyword = _id_keyword(specification)
        raise _id_unresolvable(identifier, specification) from _Malformed(f"is not a string: {'an id' if keyword == 'id' else 'an $id'} is a URI reference")
    return identifier


def _moved(base: str, schema: Any, specification: referencing.Specification[Any]) -> str:
    """The base URI inside ``schema``, reached under ``base``: its ``$id``
    joined to ``base``, as referencing's ``Resolver.in_subresource`` joins
    it, or ``base`` itself. An `_IdUnresolvable` for one that is not a
    string (`_schema_id`) or that urljoin cannot join (``http://[x``).

    An empty fragment (``https://example.com/user.json#``, which the
    2019-09 and 2020-12 meta-schemas allow) is dropped first, as
    ``Resource.id`` drops it, and as the registry keys a resource: joined
    as written, an absolute ``$id``'s ``#`` stayed on the base, which no
    resource is kept under, so a lookup jsonschema makes itself (the
    ``unevaluated*`` keywords', a ``$recursiveRef``) went to retrieval, and
    was refused as a remote document."""
    identifier = _schema_id(schema, specification)
    if identifier is None:
        return base
    try:
        return urljoin(base, identifier.rstrip("#"))
    except ValueError as e:
        raise _id_unresolvable(identifier, specification) from e


def _id_keyword(specification: referencing.Specification[Any]) -> str:
    return "id" if specification in (referencing.jsonschema.DRAFT3, referencing.jsonschema.DRAFT4) else "$id"


def _id_unresolvable(identifier: Any, specification: referencing.Specification[Any]) -> referencing.exceptions.Unresolvable:
    """An ``$id`` (``id`` in Draft 3 and 4) that cannot be read, to raise
    from why."""
    return (_LegacyIdUnresolvable if _id_keyword(specification) == "id" else _IdUnresolvable)(ref=identifier)


class _Probe:
    """Stands in for a resolver to ``Specification.maybe_in_subresource``,
    to tell whether it enters the subresource it is given: whether JSON
    Schema reads a schema there (`_entered`)."""

    __slots__ = ("entered",)

    def __init__(self) -> None:
        self.entered = False

    def in_subresource(self, subresource: Any) -> "_Probe":
        self.entered = True
        return self


class _DynamicRefUnresolvable(referencing.exceptions.Unresolvable):
    """An Unresolvable for a ``$dynamicRef``, so a message names that keyword
    (`BodySchema.why_unresolvable`)."""


class _IdUnresolvable(referencing.exceptions.Unresolvable):
    """An ``$id`` that cannot be read (`_moved`), so a message names it, as
    `validate --deep` names one further in."""


class _LegacyIdUnresolvable(_IdUnresolvable):
    """An ``id`` that cannot be read, in Draft 3 or 4, which call it that."""


# The keyword each of these names, for `BodySchema.why_unresolvable`:
# referencing's own Unresolvable is a `$ref`'s (`_unresolvable`).
_NAMED_BY: dict[type[referencing.exceptions.Unresolvable], str | None] = {
    referencing.exceptions.Unresolvable: None,
    _DynamicRefUnresolvable: "$dynamicRef",
    _IdUnresolvable: "$id",
    _LegacyIdUnresolvable: "id",
}


def _unresolvable(keyword: str, ref: Any) -> referencing.exceptions.Unresolvable:
    return (_DynamicRefUnresolvable if keyword == "$dynamicRef" else referencing.exceptions.Unresolvable)(ref=ref)


class _Refused(Exception):
    """Why a reference was not followed to a document, worded to follow the
    reference as written (``$ref 'common.json' names ...``). ``missing`` is a
    local file that does not exist."""

    def __init__(self, message: str, *, missing: bool = False) -> None:
        super().__init__(message)
        self.missing = missing


class _Malformed(Exception):
    """Why a reference or an ``$id`` cannot be read at all, worded to follow
    it as written (``$ref 5 is not a string: ...``)."""


class _Same:
    """An object as a cache key, by identity: a registry resolver, a schema
    dict. It holds the object, so the id cannot be another's while kept."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __hash__(self) -> int:
        return id(self.value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Same) and other.value is self.value


class _LocalFiles:
    """How a body schema's registry retrieves a document it does not hold
    yet: a local file, within the reference root.

    referencing calls it with the reference resolved against the base URI it
    was written under, so a relative reference arrives as the ``file:`` URI of
    a path relative to the document it is in. Anything else is refused, a
    remote URI above all, a file on another host too: nothing is fetched,
    and nothing is touched on the filesystem before that is settled. What it
    raises, referencing chains under its own Unresolvable
    (`BodySchema.why_unresolvable`).

    A document is read once per verify step: referencing adds what it
    retrieves only to the registry of the lookup that retrieved it, so the
    same file is asked for again by the next lookup, for each item of an array
    a ``$ref`` validates.
    """

    __slots__ = ("_retrieved", "_root", "_specification", "names")

    def __init__(self, root: Path | None, specification: referencing.Specification[Any]) -> None:
        self._root = root.resolve() if root is not None else None
        self._specification = specification
        self._retrieved: dict[str, referencing.Resource[Any]] = {}
        # A document's name for a message, by the id of its contents.
        self.names: dict[int, str] = {}

    def __call__(self, uri: str) -> referencing.Resource[Any]:
        resource = self._retrieved.get(uri)
        if resource is None:
            resource = self._retrieved[uri] = _resource(self._document(uri), self._specification)
        return resource

    def name(self, resource: referencing.Resource[Any]) -> str:
        """Where a reference looked, for a message: the file a document came
        from, or the resource an ``$id`` names, which a ``#...`` inside it is
        relative to, a component's in an OpenAPI document too."""
        name = self.names.get(id(resource.contents))
        if name is not None:
            return name
        identifier = resource.id() if isinstance(resource.contents, dict) else None
        return f"the schema whose $id is {identifier!r}" if identifier is not None else "the document it points into"

    def _document(self, uri: str) -> Any:
        parts = urlsplit(uri)
        shown = _shown(uri)
        if parts.scheme in ("http", "https"):
            raise _Refused(f"names {shown}, a remote document: remote references are not fetched, so keep it in a local file")
        if parts.scheme != "file":
            raise _Refused(f"names {shown}, which is not a local file, and nothing else is read")
        # A file on another host: a UNC path on Windows (\\host\share\x.json),
        # which merely checking that it exists would open an SMB connection
        # for, credentials and all. Refused before anything touches the
        # filesystem: `file://host/...`, `file:////host/...`, backslashes. A
        # `$ref` written so is refused before (`_file_refusal`); a base an
        # `$id` sets (`"$id": "file://host/share/"`) is not.
        if parts.netloc not in ("", "localhost") or unquote(parts.path).replace("\\", "/").startswith("//"):
            raise _Refused(f"names {shown}, a file on another host: nothing is read over the network, so keep it in a local file")
        try:
            path = Path.from_uri(uri)
            # A name no file can have, a lone surrogate: Python 3.13 refuses
            # it converting the URI, 3.14 only once the path is used, where
            # exists() would read it as a file that is not there.
            str(path).encode("utf-8")
        except ValueError as e:
            raise _Refused(f"names {shown}, which is not a local file path: {e}") from e
        # As a scenario's $include has it: a file that is not there is not
        # found, wherever it would be, and one that is there but outside the
        # root is refused.
        named = _shown(str(path))
        if not path.exists():
            raise _Refused(f"names {named}, which does not exist", missing=True)
        if self._root is not None and not path.resolve().is_relative_to(self._root):
            raise _Refused(f"names {named}, outside the reference root {self._root}: a schema's references must stay within it, as a scenario's $include must")
        try:
            document = read_schema_document(path)
        except SchemaFileError as e:
            raise _Refused(f"names {named}, which cannot be read as JSON: {e}") from e
        self.names[id(document)] = str(path)
        return document


def _specification_of(document: Any, default: referencing.Specification[Any]) -> referencing.Specification[Any]:
    """The specification a document is a resource of, as
    ``Resource.from_contents`` has it: the dialect its ``$schema`` names, else
    ``default``. referencing reads a ``$schema`` that is not a string as a
    dialect name and fails on it; one is the meta-check's to refuse (or, in a
    document a reference reaches, the keyword's)."""
    if _declares_dialect(document):
        return referencing.jsonschema.specification_with(document["$schema"], default=default)
    return default


def _resource(document: Any, specification: referencing.Specification[Any]) -> referencing.Resource[Any]:
    """A document as a registry resource, of `_specification_of`'s specification."""
    return _specification_of(document, specification).create_resource(document)


def _specification(dialect: Dialect) -> referencing.Specification[Any]:
    """A dialect's ``referencing`` specification: how it finds ``$id``s,
    anchors and subschemas."""
    return referencing.jsonschema.specification_with(dialect.ID_OF(dialect.META_SCHEMA) or "", default=referencing.jsonschema.DRAFT202012)


def _declared_dialect(schema: Any, dialect: Dialect) -> Dialect:
    """The dialect jsonschema validates ``schema`` under when it reaches it
    under ``dialect``: its own ``$schema``, when it declares one."""
    if _declares_dialect(schema):
        return jsonschema.validators.validator_for(schema, default=dialect)
    return dialect


def _shown(text: str) -> str:
    """``text`` for a message, a NUL or a lone surrogate in it (one JSON \\u
    escape away) escaped: a terminal cannot show the one, nor a UTF-8 stream
    encode the other."""
    return text if text.isprintable() else repr(text)[1:-1]


def _quoted(value: Any) -> str:
    """A reference or an ``$id`` as written, for a message: a string quoted,
    as ``repr`` quotes it, anything else as the JSON it is (``5``,
    ``{"type": "string"}``), cut short past a line's worth."""
    if isinstance(value, str):
        return repr(value)
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        # Not JSON: what a template rendered into an inline schema.
        return f"(a {type(value).__name__})"
    return text if len(text) <= _QUOTED_MAX else f"{text[: _QUOTED_MAX - 3]}..."


_QUOTED_MAX = 60


def _dict_ids(document: Any) -> set[int]:
    """The ids of the objects in ``document``, however deep."""
    ids: set[int] = set()
    stack = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            ids.add(id(node))
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return ids


def _file_uri(path: Path) -> str:
    """A file's ``file:`` URI, its path made absolute (``..`` folded, as a
    URI's are) but not resolved: a relative reference is relative to where
    the file is found, a symlink's directory for a symlink, as with
    ``$include``, and the root is checked on the resolved path."""
    return Path(os.path.abspath(path)).as_uri()


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _json_kind(value: Any) -> str:
    """What a JSON value is, for a pointer that cannot go into it."""
    match value:
        case None:
            return "null"
        case bool():
            return "a boolean"
        case int() | float():
            return "a number"
        case str():
            return "a string"
        case _:
            return f"a {type(value).__name__}"
