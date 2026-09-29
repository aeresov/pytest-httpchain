"""What a stage's ``verify`` and ``save`` steps mean.

Pure functions over ``(model, response)`` — no chain state — raising
`VerificationError` / `SaveError` on failure: a verify step once it has run
every check, naming all that failed, a save step at its first error. A save
step comes resolved; a verify step comes as declared, with the carrier's way
to render each check's value (`VerifyRender`). The carrier owns the sequence,
and a step that fails ends it.
"""

import json
import operator
import re
from collections import ChainMap
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import httpx
import jmespath
import jmespath.exceptions
import jmespath.functions
import jsonschema
import pytest
import referencing.exceptions

from pytest_httpchain.body_schema import UNBOUNDED, InvalidReferencedSchema, ReferenceBounds, SchemaFile, file_body_schema, inline_body_schema
from pytest_httpchain.errors import SaveError, SchemaFileError, SchemaPointerError, StageExecutionError, VerificationError
from pytest_httpchain.jsonref import json_equal
from pytest_httpchain.models import (
    JSON_TYPE_NAMES,
    HeaderMatcher,
    JMESPathMatcher,
    JMESPathSave,
    RegexCapture,
    RegexSave,
    Save,
    SubstitutionsSave,
    UserFunctionsSave,
    Verify,
    is_status_class,
    regex_group,
)
from pytest_httpchain.redaction import DEFAULT_REDACTION, REDACTED, Redaction
from pytest_httpchain.templates import TemplatesError, contains_template
from pytest_httpchain.userfunc import UserFunctionError, call_target, call_user_function
from pytest_httpchain.utils import optional_as_list, path_segment, process_substitutions, schema_error_text


def process_save(save_model: Save, response: httpx.Response, context: ChainMap[str, Any]) -> dict[str, Any]:
    """Extract one save step's ``{name: value}`` contribution to the context."""
    step_saved: dict[str, Any] = {}

    match save_model:
        case JMESPathSave():
            response_json = _response_json(response, SaveError, "extract variables")
            for var_name, jmespath_expr in save_model.jmespath.items():
                try:
                    step_saved[var_name] = jmespath.search(jmespath_expr, response_json)
                except jmespath.exceptions.JMESPathError as e:
                    raise SaveError(f"Error saving variable {var_name}: {e}") from e

        case RegexSave():
            for var_name, entry in save_model.regex.items():
                step_saved[var_name] = _regex_save(var_name, entry, response.text)

        case SubstitutionsSave():
            try:
                step_saved.update(process_substitutions(save_model.substitutions, context))
            except TemplatesError as e:
                # The step's own templates: no other response changes them.
                raise SaveError(f"Error processing substitutions: {e}", retryable=False) from e

        case UserFunctionsSave():
            for func_item in save_model.user_functions:
                try:
                    func_result = call_user_function(func_item, response=response)
                except UserFunctionError as e:
                    raise SaveError(f"Error calling user function '{func_item}': {e}", retryable=_raised_retryable(e)) from e

                if not isinstance(func_result, dict):
                    raise SaveError(f"Save function must return dict, got {type(func_result).__name__}")
                step_saved.update(func_result)

        case _:
            raise RuntimeError(f"Unhandled save type: {type(save_model).__name__}")

    return step_saved


def _regex_save(name: str, entry: str | RegexCapture, text: str) -> Any:
    """What one ``save.regex`` entry saves from the body's ``text``: its
    group (`regex_group`) in the first match, or with ``all`` a list of it
    from every match, empty when there is none. A group that took no part in
    its match is None, as ``re`` has it.

    The entry comes rendered and validated, its literal pattern's group
    checked at load. A template can render to template text, though, which
    each field's template branch takes as it is: a pattern that ``re``
    refuses, a group or an ``all`` that is none. Each fails the step here,
    as does a group the pattern a template rendered does not have, checked
    before any match is tried so that ``all`` finding nothing cannot hide it.
    """
    pattern, group, every = (entry.pattern, entry.group, entry.all) if isinstance(entry, RegexCapture) else (entry, None, False)
    # Each refusal is of what the step's templates rendered, as a value that
    # does not validate is (`carrier._render_save`): not retryable.
    if not isinstance(every, bool):
        raise SaveError(f"Error saving variable {name}: all must resolve to true or false, got {every!r}", retryable=False)
    if isinstance(group, str) and not group.isidentifier():
        raise SaveError(f"Error saving variable {name}: group must resolve to a group's number or name, got {group!r}", retryable=False)
    try:
        compiled = re.compile(pattern)
    except (re.error, OverflowError, RecursionError) as e:
        raise SaveError(f"Error saving variable {name}: pattern must resolve to a regular expression, got {pattern!r} ({e})", retryable=False) from e
    try:
        chosen = regex_group(compiled, group)
    except ValueError as e:
        raise SaveError(f"Error saving variable {name}: {e}", retryable=False) from e
    if every:
        return [match.group(chosen) for match in compiled.finditer(text)]
    match = compiled.search(text)
    if match is None:
        raise SaveError(f"Error saving variable {name}: regex '{pattern}' does not match the response body")
    return match.group(chosen)


# Where a value is declared in a verify step: ``("status",)``, ``("headers",
# "Location")``, ``("body", "contains", 2)``.
type VerifyWhere = tuple[str | int, ...]


@dataclass(frozen=True, slots=True)
class RenderFailure:
    """A value of a verify step that did not render, and why: the
    `VerificationError` that is its check's failure (its message, and what
    caused it)."""

    error: VerificationError


@dataclass(frozen=True, slots=True)
class RenderOutcome:
    """A value of a verify step whose rendering raised a pytest outcome: a
    function a template calls skipped, xfailed or failed."""

    outcome: BaseException


# How `process_verify` gets the values its checks run on: ``render(values)``
# takes the step's values, each where it is declared, in the order their
# checks run (`_declared_values`), and gives back, by where, what each
# rendered to: the value with its templates rendered, or a `RenderFailure`. A
# value whose rendering raised a pytest outcome is a `RenderOutcome`, and the
# values after it are left out: the rendering ends there.
type VerifyRender = Callable[[list[tuple[VerifyWhere, Any]]], dict[VerifyWhere, Any]]


def process_verify(
    verify_model: Verify,
    response: httpx.Response,
    scenario_dir: Path | None = None,
    redaction: Redaction = DEFAULT_REDACTION,
    render: VerifyRender | None = None,
    ref_bounds: ReferenceBounds = UNBOUNDED,
) -> None:
    """Run one verify step's assertions, raising one `VerificationError` that
    names every check that failed.

    A failed check does not stop the step, where stopping cost one run per
    wrong assertion to fix a scenario. The checks run in this order, the one
    docs/usage/responses.md documents, and their failures are listed in it:
    status, headers, jmespath, expressions, user_functions, body.schema, then
    the body's contains, not_contains, matches and not_matches. Headers,
    jmespath entries, expressions, user functions and body operands run in the
    order they are written, and each is a check of its own; so is each field
    a header matcher sets, in the order contains, not_contains, matches,
    not_matches, and each key a `JMESPathMatcher` sets, in the model's order.
    One failure reads as it always has, several are listed under a count
    (`_Failures`).

    ``verify_model`` is the step as declared, and ``render`` renders its
    values (the carrier's renders them in the response step's context);
    without one, the model is taken as rendered already. Every value is
    rendered once, before the first check runs (`_RenderedStep`). A value
    whose templates cannot be rendered is one failure of the step, listed in
    its check's place, and its check does not run: the others still do.

    A check that cannot run fails once, not once per assertion it held: the
    body is parsed as JSON at most once for the step, by the first check that
    reads it, and one that is not JSON is one failure however many of the
    step's checks wanted it, jmespath entries and body.schema (`_JsonBody`).

    A user function's pytest.skip(), xfail() or fail() ends the step there, as
    it ends the stage, and so does one that a function a template calls
    raises, once the checks reach that template. If the step has failures by
    then, those are what it raises: the checks before it that failed, and
    every value of the step that did not render, the ones after it included,
    as does a template after it that called pytest.fail(). The function ran
    only because a failure no longer stops the step, and a template that fails
    fails the stage wherever it sits, as it did when the step was rendered
    whole before any check: neither may be turned into a skipped or xfailed
    stage. A fail() is listed with them, in its place; a skip or an xfail is
    dropped. A fail() that is the step's one failure is raised as itself, as
    it always was.

    A header check's message shows the header's value through ``redaction``, as
    the report does, and so does an exact-match expected value: it is a whole
    value of that header. A matcher operand shows as written unless its failure
    would echo what the redaction hides (`text_matcher_failures`).

    ``scenario_dir`` is what a body schema's file path and an inline schema's
    references to files are relative to, and ``ref_bounds`` what a schema's
    references to files are held to (`body_schema.ReferenceBounds`): the
    carrier passes pytest's rootdir and the parent traversal depth, which
    bound a scenario's ``$include`` too. Without them, a relative reference
    may name any local file.
    """
    failures = _Failures()
    body = _JsonBody(response, failures)
    step = _RenderedStep(verify_model, render)

    def end_step(outcome: BaseException, raised_by: str, where: VerifyWhere) -> NoReturn:
        """End the step at ``where`` on a pytest outcome ``raised_by`` a
        function: the outcome itself, or the failures the step has by then.

        Those past the checks that ran: the outcome, if a fail(), then each
        value after ``where`` that did not render, or whose template called
        pytest.fail() (nothing after that one was rendered)."""
        ending: list[tuple[str, BaseException | None, BaseException | None]] = []
        if _is_fail(outcome):
            ending.append((f"{raised_by} called pytest.fail(): {outcome}", None, outcome))
        for later_where, value in step.after(where):
            match value:
                case RenderFailure(error=error):
                    ending.append((str(error), error.__cause__, None))
                case RenderOutcome(outcome=later) if _is_fail(later):
                    ending.append((f"{_template_at(later_where)} called pytest.fail(): {later}", None, later))
        if not failures and not ending:
            raise outcome
        if not failures and len(ending) == 1 and (fail := ending[0][2]) is not None:
            # Raised where a function's skip or xfail is being handled: `from`
            # keeps that outcome out of the report, which would otherwise open
            # with it, as the aggregate's does (`_Failures.error`).
            raise fail from fail.__cause__
        # Each a value that did not render or a pytest.fail(), neither of
        # which a stage's retry retries.
        for message, cause, _outcome in ending:
            failures.add(message, cause=cause, retryable=False)
        error = failures.error()
        raise error from error.__cause__

    def rendered(*where: str | int) -> Any:
        """The value declared at ``where``, as its check runs on it, or
        `_UNRENDERED` once the step has its failure to render."""
        match value := step.value(where):
            case RenderFailure(error=error):
                failures.add(str(error), cause=error.__cause__, retryable=False)
                return _UNRENDERED
            case RenderOutcome(outcome=outcome):
                end_step(outcome, _template_at(where), where)
        return value

    if verify_model.description is not None:
        # No check reads it, but it is rendered as every template of the step
        # is: the validator checks the names it uses, as it checks the others'.
        rendered("description")

    # `is not None`, not truthiness: None means undeclared, and only that — the
    # carrier refuses an assertion a template rendered to None.
    if verify_model.status is not None and (status := rendered("status")) is not _UNRENDERED:
        failures.add(_status_failure(status, response.status_code))

    for header_name in verify_model.headers:
        expected_value = rendered("headers", header_name)
        if expected_value is _UNRENDERED:
            continue
        match expected_value:
            case HeaderMatcher():
                # An absent header behaves as an empty string, as bodies do.
                actual = response.headers.get(header_name) or ""
                shown = redaction.header(header_name, actual)
                failures.extend(
                    text_matcher_failures(
                        f"Header '{header_name}' (value: {shown!r})",
                        actual,
                        contains=optional_as_list(expected_value.contains),
                        not_contains=optional_as_list(expected_value.not_contains),
                        matches=optional_as_list(expected_value.matches),
                        not_matches=optional_as_list(expected_value.not_matches),
                        shown=shown,
                    )
                )
            case _:
                actual = response.headers.get(header_name)
                if actual != expected_value:
                    shown = redaction.header(header_name, actual) if actual is not None else None
                    failures.add(f"Header '{header_name}' doesn't match: expected {redaction.header(header_name, expected_value)}, got {shown}")

    # Before the expressions, where the save + expression it replaces checked
    # the body: this is that check, without the save. The body is parsed by the
    # first entry that renders.
    for expression in verify_model.jmespath:
        expected = rendered("jmespath", expression)
        if expected is not _UNRENDERED and (parsed := body.parsed("check verify.jmespath")) is not _UNPARSABLE:
            _verify_jmespath(expression, expected, parsed, failures)

    for i in range(len(verify_model.expressions)):
        # An expression is a predicate, not a value. Truthiness alone would pass a
        # stage on "{{ response.status }}" against a 500, and an entry that
        # rendered away to None needs nothing from the carrier's rendered-away
        # guard (which watches model fields, not list items): it is a non-bool,
        # and fails right below.
        expression = rendered("expressions", i)
        if expression is _UNRENDERED:
            continue
        if not isinstance(expression, bool):
            # The type alone, never the value, as for skip_if: `{{
            # response.headers['Set-Cookie'] }}` renders a credential, which
            # the report redacts in the headers but this line would print.
            failures.add(f"Verify expression {i} must evaluate to bool, got {type(expression).__name__}, a value written where a condition belongs")
        elif not expression:
            failures.add(f"Expression {i} failed: evaluated to {expression}")

    for i in range(len(verify_model.user_functions)):
        func_item = rendered("user_functions", i)
        if func_item is _UNRENDERED:
            continue
        # How a function is named among several failures. A lone failure keeps
        # the words it always had, which print the model.
        named = f"'{call_target(func_item)[0]}' (user_functions[{i}])"
        try:
            result = call_user_function(func_item, response=response)
        except UserFunctionError as e:
            failures.add(f"Error calling user function '{func_item}': {e}", cause=e, listed=f"Error calling user function {named}: {e}", retryable=_raised_retryable(e))
            continue
        except (pytest.skip.Exception, pytest.fail.Exception) as e:
            end_step(e, f"Function {named}", ("user_functions", i))

        if not isinstance(result, bool):
            got = type(result).__name__
            failures.add(f"Verify function must return bool, got {got}", listed=f"Function {named} must return bool, got {got}")
        elif not result:
            failures.add(f"Function '{func_item}' verification failed", listed=f"Function {named} verification failed")

    if verify_model.body.schema is not None and (schema := rendered("body", "schema")) is not _UNRENDERED:
        _verify_body_schema(schema, body, scenario_dir, ref_bounds, failures)

    def operands(field: str) -> Iterator[Any]:
        """The body operands of ``field`` that rendered, a failure to render
        listed in its place."""
        for i in range(len(getattr(verify_model.body, field))):
            if (operand := rendered("body", field, i)) is not _UNRENDERED:
                yield operand

    failures.extend(
        text_matcher_failures(
            "Body",
            response.text,
            contains=operands("contains"),
            not_contains=operands("not_contains"),
            matches=operands("matches"),
            not_matches=operands("not_matches"),
        )
    )

    if failures:
        raise failures.error()


# What `process_verify` has for a value whose templates did not render: its
# failure is recorded, and its check does not run.
_UNRENDERED = object()

# The body's operand lists, in the order their checks run.
_BODY_OPERANDS = ("contains", "not_contains", "matches", "not_matches")


def _is_fail(outcome: BaseException) -> bool:
    """Whether a pytest outcome is a fail(): an XFailed is a Failed too."""
    return isinstance(outcome, pytest.fail.Exception) and not isinstance(outcome, pytest.xfail.Exception)


def _template_at(where: VerifyWhere) -> str:
    """A template's value named for a failure: ``The template at
    'verify.expressions[0]'``."""
    return f"The template at 'verify{''.join(map(path_segment, where))}'"


def _declared_values(verify_model: Verify) -> Iterator[tuple[VerifyWhere, Any]]:
    """Each value of a verify step that is rendered (`_RenderedStep`), where
    it is declared, in the order `process_verify` runs their checks: a
    header's expectation and a jmespath entry's whole, a list item by item."""
    if verify_model.description is not None:
        yield ("description",), verify_model.description
    if verify_model.status is not None:
        yield ("status",), verify_model.status
    for name, expected in verify_model.headers.items():
        yield ("headers", name), expected
    for expression, expected in verify_model.jmespath.items():
        yield ("jmespath", expression), expected
    for i, expression in enumerate(verify_model.expressions):
        yield ("expressions", i), expression
    for i, function in enumerate(verify_model.user_functions):
        yield ("user_functions", i), function
    if verify_model.body.schema is not None:
        yield ("body", "schema"), verify_model.body.schema
    for field in _BODY_OPERANDS:
        for i, operand in enumerate(getattr(verify_model.body, field)):
            yield ("body", field, i), operand


class _RenderedStep:
    """A verify step's values as its checks run on them: each rendered once,
    all before the first check runs (`VerifyRender`), kept by where it is
    declared, in check order.

    Rendered check by check instead, a user function's skip or xfail ended the
    step before the templates after it had been looked at, and a stage whose
    template failed passed as skipped. Rendered up front, a value that did not
    render is kept as its failure (`RenderFailure`), both for its check's
    place in the list and for an outcome that ends the step before that place
    (`after`). An outcome a template raises ends the rendering there
    (`RenderOutcome`): the step ends when its checks reach it, so nothing
    after it is rendered, as nothing after it was when the step rendered whole.
    """

    __slots__ = ("_values",)

    def __init__(self, verify_model: Verify, render: VerifyRender | None) -> None:
        declared = list(_declared_values(verify_model))
        if render is None:
            self._values: dict[VerifyWhere, Any] = dict(declared)
        else:
            rendered = render(declared)
            self._values = {where: rendered[where] for where, _ in declared if where in rendered}

    def value(self, where: VerifyWhere) -> Any:
        """What the value declared at ``where`` rendered to, or why it did not."""
        return self._values[where]

    def after(self, where: VerifyWhere) -> list[tuple[VerifyWhere, Any]]:
        """The values declared after ``where`` that were rendered, in order,
        each where it is declared."""
        items = list(self._values.items())
        return items[list(self._values).index(where) + 1 :]


class _Failures:
    """The failed checks of one verify step, in the order they ran, raised as
    one `VerificationError` when the step is done.

    One failure is raised as its own message, as when a step stopped at its
    first, so a single wrong assertion reads as it always has. Several are
    counted on a first line (``3 verification checks failed:``), then listed
    one numbered line each; a message of several lines (a schema error's)
    keeps its later lines, indented under its first.

    The error is retryable (a stage's ``retry`` may attempt the stage again)
    unless one of its failures is not: one of the scenario's own, which the
    next attempt would repeat (a value that did not render, or rendered one
    its check cannot take, an `_Unusable`; a body schema that cannot be read;
    a user function that cannot be called, or crashed), or a pytest.fail(),
    which ends the stage wherever it is raised.
    """

    __slots__ = ("_items", "_retryable")

    def __init__(self) -> None:
        self._items: list[tuple[str, BaseException | None, str]] = []
        self._retryable = True

    def __bool__(self) -> bool:
        return bool(self._items)

    def add(self, message: str | None, *, cause: BaseException | None = None, listed: str | None = None, retryable: bool = True) -> None:
        """Record a failed check's ``message``, if any: None is a check that passed.
        ``cause`` is what the check caught, chained when the failure is the only one.
        ``listed`` is how the failure reads among several, where it differs:
        one that must say which of several like it failed (a user function).
        ``retryable`` is False for a failure another attempt would not change,
        as an `_Unusable` message is."""
        if message is not None:
            self._items.append((message, cause, message if listed is None else listed))
            self._retryable = self._retryable and retryable and not isinstance(message, _Unusable)

    def extend(self, messages: Iterable[str]) -> None:
        for message in messages:
            self.add(message)

    def error(self) -> VerificationError:
        """The step's failure, for a step with at least one. Its message is
        the whole report: nothing is chained to it but the cause of a lone
        failure, and no exception being handled where it is raised."""
        if len(self._items) == 1:
            [(message, cause, _listed)] = self._items
        else:
            lines = [f"{len(self._items)} verification checks failed:"]
            for number, (_message, _cause, item) in enumerate(self._items, start=1):
                first, *rest = item.split("\n")
                marker = f"  {number}. "
                lines.append(f"{marker}{first}")
                lines.extend(f"{' ' * len(marker)}{line}" if line else "" for line in rest)
            message, cause = "\n".join(lines), None
        error = VerificationError(message, retryable=self._retryable)
        # Set even to None: that suppresses the context too.
        error.__cause__ = cause
        return error


class _Unusable(str):
    """A check's failure message saying the value one of the step's templates
    rendered is not one the check can take: template text where a status, a
    number, a JSON type, a length or a regular expression belongs, which the
    field's template branch took as it is. It is the scenario's, as a value
    that does not validate is, so a stage's ``retry`` does not retry the step
    (`_Failures.add`); a prefix written onto one keeps the class.
    """

    __slots__ = ()


def _raised_retryable(error: UserFunctionError) -> bool:
    """Whether a verify or save function whose call failed with ``error`` may
    pass on the next attempt: when what it raised is itself a retryable stage
    failure, a `VerificationError` or `SaveError` the function raises to say
    the response is not what it waits for yet. One that could not be imported
    or found, or that raised anything else (a KeyError, a TypeError), fails
    every attempt alike, and a crash is the function's to fix."""
    cause = error.__cause__
    return isinstance(cause, StageExecutionError) and cause.retryable


def _status_failure(expected: Any, actual: int) -> str | None:
    """``verify.status``: a code or a class (``"2xx"``), or a list of them, any
    one of which passes. Why ``actual`` fails it, or None.

    Every entry is checked before any is matched: a list must not pass on one
    entry while another could never have matched. Text other than a class is a
    template that rendered to template text, which the model's template branch
    takes as it is (a client setting's does too, `request_builder`).
    """
    accepted = expected if isinstance(expected, list) else [expected]
    for entry in accepted:
        if isinstance(entry, str) and not is_status_class(entry):
            return _Unusable(f"verify.status must resolve to a status code or a class such as 2xx, got {entry!r}")
    if any(actual // 100 == int(entry[0]) if isinstance(entry, str) else actual == entry for entry in accepted):
        return None
    shown = f"one of [{', '.join(map(str, accepted))}]" if isinstance(expected, list) else str(expected)
    return f"Status code doesn't match: expected {shown}, got {actual}"


# A `_JsonBody` no check has read yet: None is a body, JSON's null.
_UNPARSED = object()
# A `_JsonBody` json could not read, which the step has a failure for.
_UNPARSABLE = object()


class _JsonBody:
    """A verify step's response body as JSON, parsed by the first check that
    reads it and kept for the next: jmespath and body.schema share one parse.

    A body json cannot read is one failure of the step, recorded by the check
    that asked first; the checks after it that need the body do not run. That
    is a ValueError: JSONDecodeError and UnicodeDecodeError are ones, and so
    is the error for an integer past Python's digit limit, which json raises
    bare. Or it is a RecursionError, for valid JSON nested deeper than json
    can parse (``[[[...]]]`` some thousand levels deep).
    """

    __slots__ = ("_failures", "_response", "_value")

    def __init__(self, response: httpx.Response, failures: _Failures) -> None:
        self._response = response
        self._failures = failures
        self._value: Any = _UNPARSED

    def parsed(self, check: str) -> Any:
        """The body as JSON, or `_UNPARSABLE`. The first time, a body that is
        not JSON is recorded as a failure saying ``check`` (``"check
        verify.jmespath"``) cannot be done on it."""
        if self._value is _UNPARSED:
            try:
                self._value = self._response.json()
            except ValueError as e:
                self._value = _UNPARSABLE
                self._failures.add(f"Cannot {check}, response is not valid JSON: {e}", cause=e)
            except RecursionError as e:
                self._value = _UNPARSABLE
                self._failures.add(f"Cannot {check}, response JSON is nested too deeply to parse: {e}", cause=e)
        return self._value


def _verify_jmespath(expression: str, expected: Any, body: Any, failures: _Failures) -> None:
    """A ``verify.jmespath`` entry: the expression's value in the parsed
    ``body`` against its expectation, a value it must equal (JSON equality) or
    a `JMESPathMatcher`, each key of which is a check of its own.

    A missing path extracts null, as JMESPath has it, so ``null`` cannot tell a
    missing key from a null one.
    """
    try:
        actual = jmespath.search(expression, body)
    except (ValueError, ArithmeticError, TypeError) as e:
        # Compiled at validation, so this is evaluation against this body.
        # jmespath's own errors (JMESPathError is a ValueError): a function
        # given the wrong type, `length(id)` on a number, or an unknown
        # function or wrong argument count, found only when it is called.
        # And Python's, from what jmespath hands its functions unchecked:
        # ceil() on 1e400, which json reads as inf (OverflowError), or on
        # NaN (ValueError); contains() on a string, looking for a number
        # (TypeError). One failure: no key of a matcher can be judged.
        failures.add(f"JMESPath {expression!r} cannot be evaluated against the response body: {_evaluation_error(e)}", cause=e)
        return
    if isinstance(expected, JMESPathMatcher):
        failures.extend(_jmespath_matcher_failures(expression, expected, actual))
    elif not json_equal(actual, expected):
        failures.add(f"JMESPath {expression!r} doesn't match: expected {_shown(expected)}, got {_shown(actual)}")


# JMESPath's names for its types, which its type error gives for an argument
# and for what a ``*_by()`` function's expression gave. For an element of an
# array argument it gives the element's Python type name (``int``, ``str``)
# instead: none of these.
_JMESPATH_TYPE_NAMES = frozenset(jmespath.functions.TYPES_MAP.values())

# The functions that sort or pick an array's elements by what an expression
# reference gives for each one.
_BY_FUNCTIONS = frozenset({"sort_by", "min_by", "max_by"})


def _evaluation_error(error: Exception) -> str:
    """Why an expression cannot be evaluated against the body.

    jmespath's type error is rebuilt from its fields: its message puts the
    value it refused whole, as a Python repr (``True``, ``None``, a whole array
    for ``keys(items)``). Here the value shows as `_shown` shows one, its type
    named as JSON names it, and the error says which of three things it is
    about. An argument, the common case: ``keys() needs object, got [1, 2]
    (array)``. An element of an array argument, which a function taking an
    array of strings or numbers checks one by one (``join()``, ``sort()``,
    ``sum()``, ``max()``), and jmespath then holds the element alone: ``join()
    needs array-string, got an array holding 1 (number)``, or ``of mixed
    types`` when the element is of a type the function takes but the first
    one is not (``max()`` of ``[1, "x"]``). Or what a ``*_by()`` function's
    expression gave for an element, whose type is all the error says for
    certain (``sort_by()`` holds the element, not what the expression gave for
    it, when it is the first one): ``max_by() needs its expression to give
    number or string for every element, got null for one``.
    """
    if not isinstance(error, jmespath.exceptions.JMESPathTypeError):
        return str(error)
    expected = " or ".join(error.expected_types)
    value = error.current_value
    if error.actual_type not in _JMESPATH_TYPE_NAMES:
        element_type = json_type(value)
        mixed = " of mixed types" if f"array-{element_type}" in error.expected_types else ""
        return f"{error.function_name}() needs {expected}, got an array{mixed} holding {_shown(value)} ({element_type})"
    if error.function_name in _BY_FUNCTIONS and not {"array", "expref"} & set(error.expected_types):
        return f"{error.function_name}() needs its expression to give {expected} for every element, got {error.actual_type} for one"
    return f"{error.function_name}() needs {expected}, got {_shown(value)} ({error.actual_type})"


_ORDERINGS = {"gt": operator.gt, "ge": operator.ge, "lt": operator.lt, "le": operator.le}


def _jmespath_matcher_failures(expression: str, matcher: JMESPathMatcher, actual: Any) -> Iterator[str]:
    """Why ``actual`` fails each key the matcher sets, in the model's order.

    A key the matcher leaves out is not a check; a key it sets is, a null
    operand included (`JMESPathMatcher.NULL_OPERANDS`).
    """
    subject = f"JMESPath {expression!r}"
    for key in type(matcher).model_fields:
        if key in matcher.model_fields_set:
            failure = _matcher_failure(key, getattr(matcher, key), actual)
            if failure is not None:
                yield type(failure)(f"{subject}{failure}")


def _matcher_failure(key: str, operand: Any, actual: Any) -> str | None:
    """Why ``actual`` fails the matcher key ``key``, or None when it passes.

    Three kinds: a mismatch (``doesn't match: expected gt 0, got -1``); an
    actual value the key cannot judge (``gt needs a number, got "5"
    (string)``), which is a failure too, never a pass; and an operand that is
    text where a number, a type, a length or a regex belongs. That last is
    template text a template rendered, which the field's template branch took
    as it is, as ``verify.status``'s does: refused by name.
    """

    def mismatch() -> str:
        return f" doesn't match: expected {key} {_shown(operand)}, got {_shown(actual)}"

    def cannot_judge(what: str) -> str:
        return f": {key} needs {what}, got {_shown(actual)} ({json_type(actual)})"

    match key:
        case "eq" | "ne":
            return None if json_equal(actual, operand) == (key == "eq") else mismatch()
        case "gt" | "ge" | "lt" | "le":
            if not is_json_type(operand, "number"):
                return _Unusable(f": {key} must resolve to a number, got {operand!r}")
            if not is_json_type(actual, "number"):
                return cannot_judge("a number")
            return None if _ORDERINGS[key](actual, operand) else mismatch()
        case "contains" | "not_contains":
            if isinstance(actual, list):
                found = any(json_equal(item, operand) for item in actual)
            elif not isinstance(actual, str | dict):
                return cannot_judge("a string, array or object")
            elif not isinstance(operand, str):
                looked_for = "on a string needs a string" if isinstance(actual, str) else "on an object needs a key (a string)"
                return f": {key} {looked_for} to look for, got {_shown(operand)} ({json_type(operand)})"
            else:
                found = operand in actual
            return None if found == (key == "contains") else mismatch()
        case "matches" | "not_matches":
            if not isinstance(actual, str):
                return cannot_judge("a string")
            try:
                found = re.search(operand, actual) is not None
            except (re.error, OverflowError, RecursionError) as e:
                # And one re cannot compile for a repeat count or a nesting too
                # big: raised out of the step, it would take the step's other
                # failures with it.
                return _Unusable(f": {key} must resolve to a regular expression, got {operand!r} ({e})")
            return None if found == (key == "matches") else mismatch()
        case "type":
            if operand not in JSON_TYPE_NAMES:
                return _Unusable(f": type must resolve to one of {', '.join(JSON_TYPE_NAMES)}, got {operand!r}")
            return None if is_json_type(actual, operand) else f" doesn't match: expected type {operand}, got {_shown(actual)} ({json_type(actual)})"
        case "length":
            if not is_json_type(operand, "integer") or operand < 0:
                return _Unusable(f": length must resolve to a non-negative integer, got {operand!r}")
            if not isinstance(actual, str | list | dict):
                return cannot_judge("a string, array or object")
            return None if len(actual) == operand else f" doesn't match: expected length {operand}, got {_shown(actual)} (length {len(actual)})"
        case _:
            raise RuntimeError(f"Unhandled JMESPath matcher key: {key}")


def json_type(value: Any) -> str:
    """The JSON type of a value as a message names it: an int and a float are
    both a number here, whichever the ``type`` matcher's ``integer`` takes.

    Only JSON gets here, the body as parsed and operands validated as JSON, and
    one thing jmespath hands back that is not: an expression reference
    (``&name``), from ``not_null(&a)`` or ``to_array(&a)``. That is an
    ``expref``, as JMESPath names its type (`_evaluation_error`)."""
    match value:
        case None:
            return "null"
        case bool():
            return "boolean"
        case int() | float():
            return "number"
        case str():
            return "string"
        case list():
            return "array"
        case dict():
            return "object"
        case _:
            return "expref"


def is_json_type(value: Any, name: str) -> bool:
    """The ``type`` matcher: whether ``value`` is of the JSON type ``name``.
    ``integer`` is a number written without a fraction or exponent (``1``, not
    ``1.0`` or ``1e0``), ``number`` any number; neither is a boolean."""
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return json_type(value) == name


# Long enough for a small object, short enough to keep a failure one line.
_SHOWN_MAX = 200


def _shown(value: Any) -> str:
    """A value as a failure message shows it: as JSON, which the scenario is
    written in (``"5"`` is a string, ``null`` a null), cut at `_SHOWN_MAX`.

    JSON gets here, the body as parsed and operands validated as JSON, with
    two exceptions. A template can render an int past Python's digit limit
    (``10 ** 5000``), which no conversion to text takes. And jmespath hands
    back an expression reference (``&name``) as it is, from ``not_null(&a)``
    or ``to_array(&a)``, or names one in a type error (``length(&a)``).
    """
    try:
        text = json.dumps(value, ensure_ascii=False)
    except ValueError:
        return f"(a {json_type(value)} too long to show)"
    except TypeError:
        return "(not JSON: holds a JMESPath expression reference)"
    return text if len(text) <= _SHOWN_MAX else f"{text[:_SHOWN_MAX]}... ({len(text)} characters)"


def _verify_body_schema(schema: Any, body: _JsonBody, scenario_dir: Path | None, ref_bounds: ReferenceBounds, failures: _Failures) -> None:
    """Validate the response body against an inline JSON Schema, or one in a
    file, where a JSON pointer may select it (`body_schema`): one check, which
    fails once, for its first violation, or for why it cannot run.

    Every failure to read the schema is one: a file that is not there or not
    JSON, a pointer that leads nowhere, a schema its meta-schema refuses, a
    ``$ref`` that does not resolve, the file named, with the pointer. Each is
    the scenario's, and not retryable: only the body failing the schema, or
    nested too deeply to validate, is this response's."""
    if isinstance(schema, str):
        # Template text a template rendered, which the field's template
        # branch takes as it is (`_status_failure` refuses its own).
        if contains_template(schema):
            failures.add(f"verify.body.schema must resolve to a JSON Schema or a schema file, got {schema!r}", retryable=False)
            return
        file = SchemaFile.locate(schema, scenario_dir)
        try:
            body_schema = file_body_schema(file, ref_bounds)
        except SchemaFileError as e:
            failures.add(f"Error reading body schema file '{file}': {e}", cause=e, retryable=False)
            return
        except SchemaPointerError as e:
            failures.add(f"Body schema pointer '#{file.fragment}' leads nowhere in file '{file.path}': {e}", cause=e, retryable=False)
            return
        try:
            body_schema.check()
        except Exception as e:
            # A SchemaError, or a crash checking the schema, as the inline
            # schema's check and `validate --deep` catch it: ``re`` refusing a
            # ``pattern`` too big to compile (OverflowError, RecursionError), or
            # the meta-validator's own recursion on a deeply nested schema,
            # which would take the step's other failures with it. The message
            # must not recurse too (see `schema_error_text`).
            failures.add(f"Invalid JSON Schema in file '{file}': {schema_error_text(e)}", cause=e, retryable=False)
            return
    else:
        # Already meta-checked, at model validation.
        body_schema = inline_body_schema(schema, scenario_dir, ref_bounds)

    response_json = body.parsed("validate schema")
    if response_json is _UNPARSABLE:
        return

    try:
        body_schema.validate(response_json)
    except jsonschema.ValidationError as e:
        failures.add(_schema_violation(e), cause=e)
    except RecursionError as e:
        # Validation recurses in Python, several frames per level of the body
        # (or of a schema that follows it down, ``"items": {"$ref": "#"}``), so
        # it gives out long before the C decoder does.
        failures.add(f"Cannot validate schema, response or schema is nested too deeply: {e}", cause=e)
    except referencing.exceptions.Unresolvable as e:
        # A $ref (or $dynamicRef) that does not resolve, or an $id that cannot
        # be read, must fail the stage cleanly, not escape as a raw traceback
        # past the abort machinery; and in words that say what it looked for,
        # where referencing's quote a whole document.
        failures.add(f"Cannot resolve a reference in {body_schema.where}: {body_schema.why_unresolvable(e)[0]}", cause=e, retryable=False)
    except InvalidReferencedSchema as e:
        # The schema was meta-checked, but not the ones its references reach,
        # whose keywords crash on a value their meta-schema refuses
        # (jsonschema's UnknownType for `"type": "strin"`): the reference's
        # target is meta-checked then, and named, as `validate --deep` names it.
        failures.add(f"Cannot validate against {body_schema.where}: {e}", cause=e, retryable=False)
    except Exception as e:
        # What validating against a referenced schema raises that neither its
        # meta-check nor its `$id`s account for: the meta-check ran out of the
        # stack a deep validation left it.
        failures.add(f"Cannot validate against {body_schema.where}, a schema it references is not valid: {schema_error_text(e)}", cause=e, retryable=False)


def _response_json(response: httpx.Response, error: type[StageExecutionError], purpose: str) -> Any:
    """The body parsed as JSON, or ``error`` naming what it was needed for, in
    the words `_JsonBody.parsed` uses for a verify step."""
    try:
        return response.json()
    except ValueError as e:
        raise error(f"Cannot {purpose}, response is not valid JSON: {e}") from e
    except RecursionError as e:
        raise error(f"Cannot {purpose}, response JSON is nested too deeply to parse: {e}") from e


def _schema_violation(error: jsonschema.ValidationError) -> str:
    """A schema violation as jsonschema words it: its message, then the
    schema and the value that failed, pretty-printed.

    Pretty-printing recurses, and stops short of a value nested some hundreds
    of levels deep, which json parses: then the message is given with where
    the value is, but not the value pretty-printed.
    """
    try:
        return f"Body schema validation failed: {error}"
    except RecursionError:
        return f"Body schema validation failed: {error.message}\n\nFailed validating {error.validator!r} at {error.json_path}: the value is nested too deeply to show"


def text_matcher_failures(
    subject: str,
    text: str,
    *,
    contains: Iterable[str],
    not_contains: Iterable[str],
    matches: Iterable[Any],
    not_matches: Iterable[Any],
    shown: str | None = None,
) -> Iterator[str]:
    """The contains/matches semantics, shared by body and header checks
    (patterns use ``re.search``): why ``text`` fails each operand, one
    failure per operand, in the order given.

    ``shown`` is ``text`` as ``subject`` shows it when a redaction hides part of
    it (a Set-Cookie's value); None, or ``text`` itself, hides nothing. A
    failed ``not_contains`` or ``not_matches`` found its operand in ``text``,
    so quoting it could echo the hidden part: it is quoted only when nothing is
    hidden or ``shown`` already holds its text (a pattern's text, not what it
    matched), and is ``[REDACTED]`` otherwise. A failed ``contains``/``matches``
    operand is not in ``text``, reveals nothing of it, and is quoted as
    written.

    A pattern ``re`` refuses fails its check, in the words a `JMESPathMatcher`
    key's does. A header matcher's can be template text a template rendered
    (``{{ ( }}``, saved from a response), which the field's template branch
    takes as it is: re.error, or OverflowError or RecursionError for a repeat
    count or a nesting too big to compile. Raised out of the step, it would
    take the step's other failures with it.
    """
    # Nothing is hidden: a header the redaction leaves alone shows as it is.
    hides = shown is not None and shown != text

    for substring in contains:
        if substring not in text:
            yield f"{subject} doesn't contain '{substring}'"

    for substring in not_contains:
        if substring in text:
            quoted = REDACTED if hides and substring not in shown else substring
            yield f"{subject} contains '{quoted}' while it shouldn't"

    for key, patterns in (("matches", matches), ("not_matches", not_matches)):
        for pattern in patterns:
            try:
                found = re.search(pattern, text) is not None
            except (re.error, OverflowError, RecursionError) as e:
                # Quoted unless its text is in the hidden part.
                quoted = REDACTED if hides and str(pattern) in text and str(pattern) not in shown else repr(pattern)
                yield _Unusable(f"{subject}: {key} must resolve to a regular expression, got {quoted} ({e})")
                continue
            if key == "matches" and not found:
                yield f"{subject} doesn't match '{pattern}'"
            elif key == "not_matches" and found:
                quoted = REDACTED if hides and str(pattern) not in shown else pattern
                yield f"{subject} matches '{quoted}' while it shouldn't"
