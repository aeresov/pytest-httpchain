"""What a stage's ``verify`` and ``save`` steps mean.

Pure functions over ``(resolved model, response)`` — no chain state — raising
`VerificationError` / `SaveError` on failure. The carrier owns the sequence.
"""

import functools
import json
import operator
import re
from collections import ChainMap
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
import jmespath
import jmespath.exceptions
import jmespath.functions
import jsonschema
import referencing.exceptions

from pytest_httpchain.errors import SaveError, SchemaFileError, VerificationError
from pytest_httpchain.jsonref import json_equal
from pytest_httpchain.models import (
    JSON_TYPE_NAMES,
    HeaderMatcher,
    JMESPathMatcher,
    JMESPathSave,
    Save,
    SubstitutionsSave,
    UserFunctionsSave,
    Verify,
    check_json_schema,
    is_status_class,
    json_schema_validator_class,
)
from pytest_httpchain.redaction import DEFAULT_REDACTION, REDACTED, Redaction
from pytest_httpchain.templates import TemplatesError
from pytest_httpchain.userfunc import UserFunctionError, call_user_function
from pytest_httpchain.utils import optional_as_list, process_substitutions, read_json_schema_file, resolve_scenario_path


def process_save(save_model: Save, response: httpx.Response, context: ChainMap[str, Any]) -> dict[str, Any]:
    """Extract one save step's ``{name: value}`` contribution to the context."""
    step_saved: dict[str, Any] = {}

    match save_model:
        case JMESPathSave():
            try:
                response_json = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise SaveError(f"Cannot extract variables, response is not valid JSON: {e}") from e

            for var_name, jmespath_expr in save_model.jmespath.items():
                try:
                    step_saved[var_name] = jmespath.search(jmespath_expr, response_json)
                except jmespath.exceptions.JMESPathError as e:
                    raise SaveError(f"Error saving variable {var_name}: {e}") from e

        case SubstitutionsSave():
            try:
                step_saved.update(process_substitutions(save_model.substitutions, context))
            except TemplatesError as e:
                raise SaveError(f"Error processing substitutions: {e}") from e

        case UserFunctionsSave():
            for func_item in save_model.user_functions:
                try:
                    func_result = call_user_function(func_item, response=response)
                except UserFunctionError as e:
                    raise SaveError(f"Error calling user function '{func_item}': {e}") from e

                if not isinstance(func_result, dict):
                    raise SaveError(f"Save function must return dict, got {type(func_result).__name__}")
                step_saved.update(func_result)

        case _:
            raise RuntimeError(f"Unhandled save type: {type(save_model).__name__}")

    return step_saved


def process_verify(verify_model: Verify, response: httpx.Response, scenario_dir: Path | None = None, redaction: Redaction = DEFAULT_REDACTION) -> None:
    """Run one verify step's assertions, raising `VerificationError` on the first failure.

    The checks run in this order, the one docs/usage/responses.md documents:
    status, headers, jmespath, expressions, user_functions, body.schema, then
    the body's contains, not_contains, matches and not_matches. Each field's
    own entries run in the order they are written. The body is parsed as JSON
    at most once for the step, by the first check that reads it (`_JsonBody`).

    A header check's message shows the header's value through ``redaction``, as
    the report does, and so does an exact-match expected value: it is a whole
    value of that header. A matcher operand shows as written unless its failure
    would echo what the redaction hides (`verify_text_matchers`).
    """
    body = _JsonBody(response)

    # `is not None`, not truthiness: None means undeclared, and only that — the
    # carrier refuses an assertion a template rendered to None before this runs.
    if verify_model.status is not None:
        _verify_status(verify_model.status, response.status_code)

    for header_name, expected_value in verify_model.headers.items():
        match expected_value:
            case HeaderMatcher():
                # An absent header behaves as an empty string, as bodies do.
                actual = response.headers.get(header_name) or ""
                shown = redaction.header(header_name, actual)
                verify_text_matchers(
                    f"Header '{header_name}' (value: {shown!r})",
                    actual,
                    contains=optional_as_list(expected_value.contains),
                    not_contains=optional_as_list(expected_value.not_contains),
                    matches=optional_as_list(expected_value.matches),
                    not_matches=optional_as_list(expected_value.not_matches),
                    shown=shown,
                )
            case _:
                actual = response.headers.get(header_name)
                if actual != expected_value:
                    shown = redaction.header(header_name, actual) if actual is not None else None
                    raise VerificationError(f"Header '{header_name}' doesn't match: expected {redaction.header(header_name, expected_value)}, got {shown}")

    # Before the expressions, where the save + expression it replaces checked
    # the body: this is that check, without the save.
    if verify_model.jmespath:
        _verify_jmespath(verify_model.jmespath, body.parsed("check verify.jmespath"))

    for i, expression in enumerate(verify_model.expressions):
        # An expression is a predicate, not a value. Truthiness alone would pass a
        # stage on "{{ response.status }}" against a 500, and an entry that
        # rendered away to None needs nothing from the carrier's rendered-away
        # guard (which watches model fields, not list items): the list keeps its
        # declared length through substitution, so it is a non-bool and fails
        # right below.
        if not isinstance(expression, bool):
            raise VerificationError(f"Verify expression {i} must evaluate to bool, got {type(expression).__name__} ({expression!r}), a value written where a condition belongs")
        if not expression:
            raise VerificationError(f"Expression {i} failed: evaluated to {expression}")

    for func_item in verify_model.user_functions:
        try:
            result = call_user_function(func_item, response=response)
        except UserFunctionError as e:
            raise VerificationError(f"Error calling user function '{func_item}': {e}") from e

        if not isinstance(result, bool):
            raise VerificationError(f"Verify function must return bool, got {type(result).__name__}")
        if not result:
            raise VerificationError(f"Function '{func_item}' verification failed")

    if verify_model.body.schema is not None:
        _verify_body_schema(verify_model.body.schema, body, scenario_dir)

    verify_text_matchers(
        "Body",
        response.text,
        contains=verify_model.body.contains,
        not_contains=verify_model.body.not_contains,
        matches=verify_model.body.matches,
        not_matches=verify_model.body.not_matches,
    )


def _verify_status(expected: Any, actual: int) -> None:
    """``verify.status``: a code or a class (``"2xx"``), or a list of them, any
    one of which passes.

    Every entry is checked before any is matched: a list must not pass on one
    entry while another could never have matched. Text other than a class is a
    template that rendered to template text, which the model's template branch
    takes as it is (a client setting's does too, `request_builder`).
    """
    accepted = expected if isinstance(expected, list) else [expected]
    for entry in accepted:
        if isinstance(entry, str) and not is_status_class(entry):
            raise VerificationError(f"verify.status must resolve to a status code or a class such as 2xx, got {entry!r}")
    if any(actual // 100 == int(entry[0]) if isinstance(entry, str) else actual == entry for entry in accepted):
        return
    shown = f"one of [{', '.join(map(str, accepted))}]" if isinstance(expected, list) else str(expected)
    raise VerificationError(f"Status code doesn't match: expected {shown}, got {actual}")


# A `_JsonBody` no check has read yet: None is a body, JSON's null.
_UNPARSED = object()


class _JsonBody:
    """A verify step's response body as JSON, parsed by the first check that
    reads it and kept for the next: jmespath and body.schema share one parse.

    A body json cannot read fails the check that asked. That is a ValueError:
    JSONDecodeError and UnicodeDecodeError are ones, and so is the error for an
    integer past Python's digit limit, which json raises bare. Or it is a
    RecursionError, for valid JSON nested deeper than json can parse
    (``[[[...]]]`` some thousand levels deep).
    """

    __slots__ = ("_response", "_value")

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self._value: Any = _UNPARSED

    def parsed(self, check: str) -> Any:
        """The body as JSON, or a `VerificationError` saying ``check`` (``"check
        verify.jmespath"``) cannot be done on it."""
        if self._value is _UNPARSED:
            try:
                self._value = self._response.json()
            except ValueError as e:
                raise VerificationError(f"Cannot {check}, response is not valid JSON: {e}") from e
            except RecursionError as e:
                raise VerificationError(f"Cannot {check}, response JSON is nested too deeply to parse: {e}") from e
        return self._value


def _verify_jmespath(expectations: dict[str, Any], body: Any) -> None:
    """``verify.jmespath``: each expression's value in the parsed ``body``
    against its expectation, a value it must equal (JSON equality) or a
    `JMESPathMatcher`.

    A missing path extracts null, as JMESPath has it, so ``null`` cannot tell a
    missing key from a null one.
    """
    for expression, expected in expectations.items():
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
            # (TypeError).
            raise VerificationError(f"JMESPath {expression!r} cannot be evaluated against the response body: {_evaluation_error(e)}") from e
        if isinstance(expected, JMESPathMatcher):
            _verify_jmespath_matcher(expression, expected, actual)
        elif not json_equal(actual, expected):
            raise VerificationError(f"JMESPath {expression!r} doesn't match: expected {_shown(expected)}, got {_shown(actual)}")


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


def _verify_jmespath_matcher(expression: str, matcher: JMESPathMatcher, actual: Any) -> None:
    """Every key the matcher sets, in the model's order, against ``actual``.

    A key the matcher leaves out is not a check; a key it sets is, a null
    operand included (`JMESPathMatcher.NULL_OPERANDS`).
    """
    subject = f"JMESPath {expression!r}"
    for key in type(matcher).model_fields:
        if key in matcher.model_fields_set:
            failure = _matcher_failure(key, getattr(matcher, key), actual)
            if failure is not None:
                raise VerificationError(f"{subject}{failure}")


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
                return f": {key} must resolve to a number, got {operand!r}"
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
            except re.error as e:
                return f": {key} must resolve to a regular expression, got {operand!r} ({e})"
            return None if found == (key == "matches") else mismatch()
        case "type":
            if operand not in JSON_TYPE_NAMES:
                return f": type must resolve to one of {', '.join(JSON_TYPE_NAMES)}, got {operand!r}"
            return None if is_json_type(actual, operand) else f" doesn't match: expected type {operand}, got {_shown(actual)} ({json_type(actual)})"
        case "length":
            if not is_json_type(operand, "integer") or operand < 0:
                return f": length must resolve to a non-negative integer, got {operand!r}"
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


def _verify_body_schema(schema: Any, body: _JsonBody, scenario_dir: Path | None) -> None:
    """Validate the response body against an inline or file-referenced JSON Schema."""
    if isinstance(schema, str | Path):
        schema_path = resolve_scenario_path(scenario_dir, schema)
        try:
            schema = read_json_schema_file(schema_path)
        except SchemaFileError as e:
            raise VerificationError(f"Error reading body schema file '{schema_path}': {e}") from e
        try:
            check_json_schema(schema)
        except jsonschema.SchemaError as e:
            raise VerificationError(f"Invalid JSON Schema in file '{schema_path}': {e}") from e

    response_json = body.parsed("validate schema")

    try:
        # Already meta-checked (inline at model validation, files just above), so
        # instantiate the dialect's validator instead of jsonschema.validate,
        # which would re-run check_schema on every stage. Either way `format`
        # is only an annotation unless a format checker is passed.
        validator_class = json_schema_validator_class(schema)
        validator_class(schema, format_checker=_format_checker(validator_class)).validate(response_json)
    except jsonschema.ValidationError as e:
        raise VerificationError(f"Body schema validation failed: {e}") from e
    except jsonschema.SchemaError as e:
        raise VerificationError(f"Invalid body validation schema: {e}") from e
    except referencing.exceptions.Unresolvable as e:
        # An unresolvable $ref inside the schema itself must fail the stage
        # cleanly, not escape as a raw traceback past the abort machinery.
        raise VerificationError(f"Cannot resolve $ref in body schema: {e}") from e


@functools.cache
def _format_checker(validator_class: type[jsonschema.protocols.Validator]) -> jsonschema.FormatChecker:
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
    for name, (func, _declared) in validator_class.FORMAT_CHECKER.checkers.items():
        checker.checks(name, raises=Exception)(func)
    return checker


def verify_text_matchers(
    subject: str,
    text: str,
    *,
    contains: Iterable[str],
    not_contains: Iterable[str],
    matches: Iterable[Any],
    not_matches: Iterable[Any],
    shown: str | None = None,
) -> None:
    """The contains/matches semantics, shared by body and header checks
    (patterns use ``re.search``).

    ``shown`` is ``text`` as ``subject`` shows it when a redaction hides part of
    it (a Set-Cookie's value). A failed ``not_contains`` or ``not_matches``
    found its operand in ``text``, so quoting it could echo the hidden part:
    it is quoted only when ``shown`` already holds its text (a pattern's text,
    not what it matched), and is ``[REDACTED]`` otherwise. A failed
    ``contains``/``matches`` operand is not in ``text``, reveals nothing of it,
    and is quoted as written.
    """
    for substring in contains:
        if substring not in text:
            raise VerificationError(f"{subject} doesn't contain '{substring}'")

    for substring in not_contains:
        if substring in text:
            quoted = substring if shown is None or substring in shown else REDACTED
            raise VerificationError(f"{subject} contains '{quoted}' while it shouldn't")

    for pattern in matches:
        if not re.search(pattern, text):
            raise VerificationError(f"{subject} doesn't match '{pattern}'")

    for pattern in not_matches:
        if re.search(pattern, text):
            quoted = pattern if shown is None or str(pattern) in shown else REDACTED
            raise VerificationError(f"{subject} matches '{quoted}' while it shouldn't")
