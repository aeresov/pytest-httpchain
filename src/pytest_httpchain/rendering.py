"""Declared scenario models rendered for the carrier: ``walk()`` and
re-validation, refusing any field a template rendered to None.

walk() re-validates what it renders, but an optional field accepts None, so a
template that rendered away would look like a field never declared: a check
skipped, a setting not applied, a stage green. `render_declared` is the guard
every model the carrier renders goes through; `verify_renderer` renders a
verify step value by value through it, `render_save` a save step, and
`rendered_match` a connection's ``expect.match``.
"""

import functools
from collections.abc import Callable, Iterator, Mapping
from typing import Any, NamedTuple

import jmespath
import pytest
from pydantic import BaseModel, RootModel, ValidationError

from pytest_httpchain.errors import SaveError, StageExecutionError, VerificationError
from pytest_httpchain.models import (
    FileSpec,
    JMESPathMatcher,
    JsonBody,
    MsgpackBody,
    RegexCapture,
    ResponseBody,
    Verify,
    validate_rendered,
    validate_rendered_expectations,
    validate_rendered_verify,
    validation_error,
)
from pytest_httpchain.redaction import DEFAULT_REDACTION, Redaction
from pytest_httpchain.response_steps import RenderFailure, RenderOutcome, VerifyRender
from pytest_httpchain.templates import TemplatesError, needs_rendering, walk, walker
from pytest_httpchain.utils import path_segment


def _none_is_a_value(model: BaseModel, field: str) -> bool:
    """The fields exempt from `render_declared`: None there is something the
    scenario can mean, not a setting that vanished — a JSON body of ``null``
    (``request_builder`` sends it as such) and free-text descriptions."""
    return field == "description" or (isinstance(model, JsonBody | MsgpackBody) and field in ("json", "msgpack"))


def _none_is_compared(model: BaseModel, field: str) -> bool:
    """The fields where a None is not "undeclared" but an operand the check
    compares with: a `JMESPathMatcher`'s eq, ne, contains and not_contains,
    where the scenario writes null to mean null. A template there that rendered
    to None is still refused (it more likely lost the value it was written
    for), but it would not have disabled the check."""
    return isinstance(model, JMESPathMatcher) and field in JMESPathMatcher.NULL_OPERANDS


def _none_is_not_set(model: BaseModel, field: str) -> bool:
    """The fields a model itself reads an explicit None in as not set: a
    `FileSpec`'s sources, of which it counts the ones not None, and takes
    exactly one. An object rendered whole holding None in another source, as
    a user function or a saved object may fill the keys it does not use,
    sends the file from the one it sets, as the same object written out does:
    the None disables nothing. Declared as a template of its own, a source
    that rendered to None leaves the file none, which `_rendered_away` reports
    through the validation error."""
    return isinstance(model, FileSpec) and field in FileSpec.SOURCES


def _none_would(model: BaseModel, field: str) -> str:
    """What a None let through ``field`` would have done, for the refusal to
    say. An optional field reads None as never declared: for most that turns
    a check or a setting off, but a regex save's group left out picks the
    default one (group 1, or the whole match), so the save still runs, from
    another group, and a multipart file's filename or content type left out
    is sent as the default one."""
    if isinstance(model, RegexCapture) and field == "group":
        return "save the default group instead"
    if isinstance(model, FileSpec) and field in ("filename", "content_type"):
        return f"send the default {field.replace('_', ' ')} instead"
    return "disable it"


# Where a value sits in a dumped model: field names and dict keys, list indices.
type _Keys = tuple[str | int, ...]

# Where a value sits in a model built to validate it, to where the scenario
# declares it (`_validate_substituted`).
type _Relocation = Callable[[_Keys], _Keys]


class _Vanished(NamedTuple):
    """A declared field a template rendered to None: where it sits, the
    template as written, whether None is an operand there
    (`_none_is_compared`), and what it would have done otherwise
    (`_none_would`)."""

    keys: _Keys
    template: str
    compared: bool = False
    would: str = "disable it"


def _rendered_away(declared: Any, substituted: Any, keys: _Keys = ()) -> Iterator[_Vanished]:
    """Each model field that was declared but rendered to None, ``keys``
    locating it in ``substituted``.

    ``substituted`` is ``declared`` dumped and substituted but not yet
    validated, so it is read alongside the declared models, which say what is a
    model field and what a dict value; dicts and lists are followed to the
    models inside them (``verify.headers`` holds its matchers in a dict). Only
    model fields count: a dict value or list item that renders to None — a query
    parameter, a user-function kwarg, a ``verify.jmespath`` value compared with
    null — is a value handed on, not a field left undeclared. Only a string can
    render to None (walk() maps containers element-wise and dumps other models
    to dicts first), so what was declared is always a template.
    """

    def field(declared_value: Any, substituted_value: Any, field_keys: _Keys, compared: bool = False, would: str = "disable it") -> Iterator[_Vanished]:
        if isinstance(declared_value, RootModel):
            # Dumped as its bare root value, not as {"root": ...}: the root is the
            # field, at the model's own place (a user-function name).
            declared_value = declared_value.root
        if declared_value is not None and substituted_value is None:
            yield _Vanished(field_keys, declared_value, compared, would)
        else:
            yield from _rendered_away(declared_value, substituted_value, field_keys)

    match declared, substituted:
        case RootModel(), _:
            yield from field(declared, substituted, keys)
        case BaseModel(), dict():
            # Only the declared fields were dumped (`render_declared`).
            for name in type(declared).model_fields:
                if name in substituted and not _none_is_a_value(declared, name):
                    yield from field(getattr(declared, name), substituted[name], (*keys, name), _none_is_compared(declared, name), _none_would(declared, name))
        case dict(), dict():
            for key, declared_value in declared.items():
                if key in substituted:
                    yield from _rendered_away(declared_value, substituted[key], (*keys, key))
        case list() | tuple(), list() | tuple():
            # Substitution rewrites a sequence element-wise, so the lengths match.
            for i, (declared_value, substituted_value) in enumerate(zip(declared, substituted, strict=True)):
                yield from _rendered_away(declared_value, substituted_value, (*keys, i))


def _rendered_whole_away(declared: Any, rendered: Any, keys: _Keys = ()) -> Iterator[_Vanished]:
    """`_rendered_away` for a model that one template rendered whole: a header
    matcher written as ``"{{ {'contains': ct, 'not_contains': 'text/html'} }}"``,
    or saved from the response and used as ``"{{ matcher }}"``, and each model
    in a list one rendered (multipart files, ``"images": "{{ files }}"``).

    Declared as a single string, such a model's fields are known only once
    validation has built it, so ``rendered`` is the validated model. A field the
    rendered mapping set explicitly (``model_fields_set``) to None is refused; one
    it left out was never declared. A field the model itself reads None in as
    not set (`_none_is_not_set`) is neither.

    A `JMESPathMatcher`, whose null operands (`_none_is_compared`) this would
    misread, is never rendered whole: a template where a ``verify.jmespath``
    value is written renders a value (`validate_rendered_verify`).
    """
    match declared, rendered:
        case BaseModel(), BaseModel() if type(declared) is type(rendered):
            for name in type(declared).model_fields:
                yield from _rendered_whole_away(getattr(declared, name), getattr(rendered, name), (*keys, name))
        case (str() as template, BaseModel()) | (RootModel(root=str() as template), BaseModel()):
            for name in type(rendered).model_fields:
                if name in rendered.model_fields_set and getattr(rendered, name) is None and not (_none_is_a_value(rendered, name) or _none_is_not_set(rendered, name)):
                    yield _Vanished((*keys, name), template, would=_none_would(rendered, name))
        case str(), list() | tuple():
            # The models are its items (a list of files does not nest), and
            # any other list a template renders (a JSON body, a query
            # parameter's values) holds none: those items are not walked.
            for i, item in enumerate(rendered):
                if isinstance(item, BaseModel):
                    yield from _rendered_whole_away(declared, item, (*keys, i))
        case dict(), dict():
            for key, declared_value in declared.items():
                if key in rendered:
                    yield from _rendered_whole_away(declared_value, rendered[key], (*keys, key))
        case list() | tuple(), list() | tuple():
            for i, (declared_value, rendered_value) in enumerate(zip(declared, rendered, strict=True)):
                yield from _rendered_whole_away(declared_value, rendered_value, (*keys, i))


def _at(structure: Any, keys: _Keys) -> Any:
    """What sits at ``keys`` in ``structure``: a model's field, a dict's value,
    a list's item."""
    for key in keys:
        structure = getattr(structure, str(key)) if isinstance(structure, BaseModel) else structure[key]
    return structure


def _replaced(structure: Any, keys: _Keys, value: Any) -> Any:
    """``structure`` with ``value`` at ``keys``, copied along the way."""
    if not keys:
        return value
    key, rest = keys[0], keys[1:]
    if isinstance(structure, dict):
        return {**structure, key: _replaced(structure[key], rest, value)}
    return type(structure)(_replaced(item, rest, value) if i == key else item for i, item in enumerate(structure))


def render_declared[M: BaseModel](
    declared: M, context: Mapping[str, Any], where: str, error: type[StageExecutionError] = StageExecutionError, validate: Callable[[Any], M] | None = None
) -> M:
    """``walk()`` a declared scenario model, refusing any field a template
    rendered to None.

    walk() re-validates what it renders, but an optional field accepts None: a
    template that rendered away — a JMESPath save of a missing key, ``get()``
    without a default — validates cleanly, and the consumer then treats the field
    as never declared. A header matcher or ``verify.status`` goes unchecked,
    ``calls_per_sec`` stops limiting, ``ssl.cert`` or ``auth`` is not applied,
    and the stage is green. "Never declared" and "declared but rendered to
    nothing" must never look the same, so every model the carrier renders goes
    through here, and a new optional field is covered without being listed:
    exempting one (`_none_is_a_value`) is the explicit choice. ``where`` names
    the model in the message; ``error`` is the failure type of the step. A model
    one template rendered whole (a header matcher written as ``"{{ matcher }}"``)
    has no declared fields to compare, so it is checked once validated
    (`_rendered_whole_away`).

    ``validate`` builds the rendered model, by default as the declared one's
    type. A model rendered on its own that was declared as one member of a
    union (a scenario's auth: the template string a user function's name
    takes can render a built-in scheme) passes the union's instead.

    The substituted form is checked before validation judges it. Where the None
    is also invalid — a matcher's only field, a required field such as ``url`` —
    pydantic's report names neither the template nor the None (a matcher's even
    asks for a field the scenario did set), so the same message is raised
    instead, without the claim that the None would have disabled anything. Which
    errors the Nones account for is settled by putting the declared templates
    back, which validated in those places before: whatever still fails is
    reported as pydantic has it, under the refusal. Where None is an operand
    (`_none_is_compared`) it validates but disables nothing either: the
    message says how to compare with null instead.
    """
    # walk()'s own model step (hands back a model with nothing to render
    # untouched; otherwise dump, substitute, re-validate), taken apart at the
    # re-validation. Only the declared fields are dumped, so the rendered model
    # keeps the declared one's `model_fields_set`: a request's timeout and
    # redirect setting count only where declared (`request_builder`), and a full
    # dump made every default look declared. A model holding an escaped `\{{`
    # and no template is rendered too: only rendering removes the escape.
    if not needs_rendering(declared):
        return declared
    return _validate_substituted(declared, walk(declared.model_dump(mode="python", exclude_unset=True), context), where, error, validate)


def _validate_substituted[M: BaseModel](
    declared: M,
    substituted: Any,
    where: str,
    error: type[StageExecutionError] = StageExecutionError,
    validate: Callable[[Any], M] | None = None,
    relocate: _Relocation | None = None,
) -> M:
    """`render_declared` from the substitution on: ``substituted`` is
    ``declared`` dumped (its declared fields) with its templates rendered,
    validated here as rendered text (by default `validate_rendered`), and
    refused where a template rendered a field to None.

    ``relocate`` maps where a value sits in ``declared`` to where its messages
    say it is, when ``declared`` holds it elsewhere than the scenario does
    (`_validated_values`): the guard's refusal and pydantic's report alike."""
    validate = validate or functools.partial(validate_rendered, type(declared))
    if relocate is not None:
        validate = _relocating(validate, relocate)
    vanished = list(_rendered_away(declared, substituted))
    try:
        rendered = validate(substituted)
    except ValidationError as e:
        if not vanished:
            raise
        refusal = _rendered_to_none(where, vanished[0], relocate)
        restored = substituted
        for keys, template, *_ in vanished:
            restored = _replaced(restored, keys, template)
        try:
            validate(restored)
        except ValidationError as other:
            raise error(f"{refusal}\n{other}") from e
        raise error(refusal) from e
    vanished = vanished or list(_rendered_whole_away(declared, rendered))
    if vanished:
        first = vanished[0]
        refusal = _rendered_to_none(where, first, relocate)
        raise error(refusal if first.compared else f"{refusal}, which would silently {first.would}")
    return rendered


def _relocation(in_part: _Keys, at: _Keys) -> _Relocation | None:
    """Keys in a verify step cut down to one value (`_verify_part`), which
    holds it at ``in_part``, as they are in the step, which holds it at
    ``at``: a list item validated alone, at index 0, keeps its own index. None
    where the two are the same place."""
    if in_part == at:
        return None
    return lambda keys: (*at, *keys[len(in_part) :]) if keys[: len(in_part)] == in_part else keys


def _relocating[M](validate: Callable[[Any], M], relocate: _Relocation) -> Callable[[Any], M]:
    """``validate``, its `ValidationError` located by ``relocate``."""

    def relocated(value: Any) -> M:
        try:
            return validate(value)
        except ValidationError as e:
            raise _relocated(e, relocate) from None

    return relocated


def _relocated(e: ValidationError, relocate: _Relocation) -> ValidationError:
    """``e``, each error at the location ``relocate`` gives its own: the same
    errors, in the same order, each of its type, message and input, rebuilt
    as pydantic wrote them (`validation_error`)."""
    errors = e.errors()
    for error in errors:
        error["loc"] = relocate(error["loc"])
    return validation_error(e.title, errors)


def verify_renderer(declared: Verify, context: Mapping[str, Any]) -> VerifyRender:
    """How `response_steps.process_verify` renders a declared verify step: value by value, so
    that a template that cannot be rendered is one failure of the step, listed
    with the others. Rendered whole, the step ended at the first, before any
    check had run.

    Each value is substituted on its own, and every one with the evaluator
    one `walker` builds from ``context`` for the step: a walk() per value
    would rebuild it from the whole context, which gains a layer per stage and
    per save step, once for each value. A template error is that value's
    failure. A pytest outcome that a function a template calls raises is that
    value's, and ends the rendering there, as it ended the whole step's. What
    was substituted is then validated (`_validated_values`).
    """

    def render(values: list[tuple[_Keys, Any]]) -> dict[_Keys, Any]:
        rendered: dict[_Keys, Any] = {}
        substituted: dict[_Keys, Any] = {}
        # Built for the first value holding a template or an escape: a step
        # without one needs neither.
        substitute: Callable[[Any], Any] | None = None
        dumped: dict[str, Any] = {}
        for at, value in values:
            if not needs_rendering(value):
                rendered[at] = value
                continue
            if substitute is None:
                substitute, dumped = walker(context), declared.model_dump(mode="python", exclude_unset=True)
            try:
                substituted[at] = substitute(_at(dumped, at))
            except TemplatesError as e:
                error = VerificationError(str(e))
                error.__cause__ = e
                rendered[at] = RenderFailure(error)
            except (pytest.skip.Exception, pytest.fail.Exception) as e:
                rendered[at] = RenderOutcome(e)
                break
        if substituted:
            rendered |= _validated_values(declared, dumped, substituted)
        return rendered

    return render


def _validated_values(declared: Verify, dumped: dict[str, Any], substituted: dict[_Keys, Any]) -> dict[_Keys, Any]:
    """Each value of ``substituted`` (by where it is declared, its templates
    substituted) as its check takes it, or a `RenderFailure`. It goes through
    the guard every declared model does (`_validate_substituted`), so a
    template that rendered to None is refused, not read as undeclared, and a
    ``verify.jmespath`` value stays a value, an object it rendered included
    (`validate_rendered_verify`). Pydantic's report becomes the step's error
    type, its message kept.

    The step is validated whole first, the values put in ``dumped`` (its
    declared fields, dumped for this rendering alone). When every value is
    valid, the common case, that is all it costs, as when the step rendered
    whole. Otherwise each value is validated in a model of it alone
    (`_verify_part`), so that each that fails is a failure of its own: a
    ``headers`` or ``jmespath`` entry alone in its map, a list item alone in
    its list, so every item of a list costs the list once, not once per item,
    failing or not, and no value is rendered twice.

    A list item's messages give its index as it is in the step
    (``body.contains.1``, ``verify.user_functions[1]``), not the 0 it has
    alone (`_relocation`). Validated again with its list whole to have that
    index, a failing item cost the list once more, and a list of templates
    that all failed took time quadratic in its length.
    """
    for at, value in substituted.items():
        _at(dumped, at[:-1])[at[-1]] = value
    try:
        step = _validated_verify(declared, dumped)
    except VerificationError:
        pass
    else:
        return {at: _at(step, at) for at in substituted}
    values: dict[_Keys, Any] = {}
    for at, value in substituted.items():
        part, in_part = _verify_part(declared, at)
        try:
            values[at] = _at(_validated_verify(part, _replaced(part.model_dump(mode="python", exclude_unset=True), in_part, value), _relocation(in_part, at)), in_part)
        except VerificationError as e:
            values[at] = RenderFailure(e)
    return values


def _validated_verify(declared: Verify, substituted: Any, relocate: _Relocation | None = None) -> Verify:
    """``substituted``, ``declared`` dumped with its templates substituted,
    validated as it rendered (`_validate_substituted`, ``relocate`` locating
    its messages), or the `VerificationError` saying why it is not valid."""
    try:
        return _validate_substituted(declared, substituted, "verify", VerificationError, functools.partial(validate_rendered_verify, declared), relocate)
    except ValidationError as e:
        raise VerificationError(str(e)) from e


def render_save[M: BaseModel](declared: M, context: Mapping[str, Any]) -> M:
    """A save step rendered through the guard (`render_declared`). A value
    it renders that does not validate is the step's `SaveError`, as a verify
    step's is its `VerificationError` (`_validated_verify`): a regex or a
    JMESPath expression a template rendered that does not compile, a group
    the pattern a template rendered does not have. Neither that nor the
    guard's refusal is retryable: it is the step's templates that failed, not
    the extraction from this response."""
    try:
        return render_declared(declared, context, "save", SaveError)
    except ValidationError as e:
        raise SaveError(str(e), retryable=False) from e
    except SaveError as e:
        e.retryable = False
        raise


# What `_verify_part` cuts a step down from: nothing set (``model_fields_set``
# empty, so a copy's holds only the field it is given), and its defaults
# resolved once. model_construct() resolves each default factory, inspecting
# its signature every time: per value rendered, that was most of the cost.
_NO_VERIFY = Verify.model_construct()
_NO_BODY = ResponseBody.model_construct()


def _verify_part(declared: Verify, at: _Keys) -> tuple[Verify, _Keys]:
    """``declared`` cut down to the value at ``at`` (the body's field, for the
    body's), and where the value sits in it: a map to the one entry, a list to
    the one item, at index 0."""
    match at:
        case ("body", str() as name, *rest):
            values, in_part = _one_value(getattr(declared.body, name), name, rest)
            return _NO_VERIFY.model_copy(update={"body": _NO_BODY.model_copy(update=values)}), ("body", *in_part)
        case (str() as name, *rest):
            values, in_part = _one_value(getattr(declared, name), name, rest)
            return _NO_VERIFY.model_copy(update=values), in_part
        case _:
            raise RuntimeError(f"Unhandled verify location: {at!r}")


def _one_value(whole: Any, name: str, rest: list[str | int]) -> tuple[dict[str, Any], _Keys]:
    """The field ``name`` holding only the value at ``rest`` within it (none:
    the field's own value), and where the value is in the field so cut."""
    match rest:
        case [str() as key]:
            return {name: {key: whole[key]}}, (name, key)
        case [int() as i]:
            return {name: [whole[i]]}, (name, 0)
        case _:
            return {name: whole}, (name,)


def _rendered_to_none(where: str, vanished: _Vanished, relocate: _Relocation | None = None) -> str:
    keys = vanished.keys if relocate is None else relocate(vanished.keys)
    path = where + "".join(map(path_segment, keys))
    message = f"'{path}' was declared as {vanished.template!r} but rendered to None"
    return f"{message}; to compare with null, write null" if vanished.compared else message


def rendered_match(declared: dict[str, Any], context: Mapping[str, Any], redaction: Redaction = DEFAULT_REDACTION) -> dict[str, Any]:
    """A connection's ``expect.match`` object with its values' templates
    rendered, all with one walker, and validated again
    (`validate_rendered_expectations`), so a value declared as a value stays
    one, whatever it rendered to. A matcher operand a template rendered to
    None is refused, as `render_declared` refuses a field's. Each failure is
    the scenario's: no attempt retries it. A value that does not validate is
    quoted by its error, so the error is shown through ``redaction``: an
    operand a template rendered from a secret, on a listed key, is hidden."""
    if not needs_rendering(declared):
        return declared
    dumped = {expression: expected.model_dump(mode="python", exclude_unset=True) if isinstance(expected, BaseModel) else expected for expression, expected in declared.items()}
    substituted = walker(context)(dumped)
    if (vanished := next(_rendered_away(declared, substituted), None)) is not None:
        raise VerificationError(_rendered_to_none("connection.expect.match", vanished), retryable=False)
    try:
        return validate_rendered_expectations(declared, substituted)
    except ValidationError as e:
        raise VerificationError(redaction.message_text(str(e), *listed_expectations(substituted, redaction)), retryable=False) from e


def listed_expectations(rendered: Mapping[str, Any] | None, redaction: Redaction) -> list[Any]:
    """What ``rendered`` expects, as `Redaction.message_text` takes messages,
    to hide where a miss quotes it: each expectation whose JMESPath
    expression reads a key ``redaction`` lists, as ``{that key: what it
    expects}`` (a value as it is, a matcher as its operands, ``{"eq":
    "s3cret"}``: a secret rendered into one is as much the key's value), and
    every expected object or array as it is, for a listed key inside it
    (``{"auth": {"eq": {"token": "s3cret"}}}``)."""
    listed: list[Any] = []
    if not rendered or not redaction.message_keys:
        return listed
    for expression, expected in rendered.items():
        dumped = expected.model_dump(mode="python", exclude_unset=True) if isinstance(expected, BaseModel) else expected
        if isinstance(dumped, dict | list):
            listed.append(dumped)
        try:
            pending: list[Any] = [jmespath.compile(expression).parsed]
        except ValueError:  # a JMESPathError: validated, so never expected
            continue
        while pending:
            node = pending.pop()
            if node.get("type") == "field" and isinstance(name := node.get("value"), str) and redaction.redacts_message_key(name):
                listed.append({name: dumped})
                break
            pending.extend(child for child in node.get("children", ()) if isinstance(child, dict))
    return listed
