"""A model's `ValidationError` as an author reads it: pydantic's errors, less
the template branch's hint where a literal branch already said what is wrong.

A field that takes a literal or a template (``GraphQLQuery |
PartialTemplateStr``, ``XMLString | PartialTemplateStr``, ...) refuses a value
neither branch takes with one error per branch: the literal's (``Invalid
GraphQL query: ...``) and the template's (``Must contain at least one template
expression ...``). Where the literal branch judged the string itself, the hint
is noise, and it misleads: written as a template, the value escapes the check
it failed. Shared by the loader (``HTTPCHAIN000``) and the models' rendered
validation (`entities.validate_rendered` and its kin), whose errors the carrier
reports as a stage's failure.
"""

from collections.abc import Iterable
from typing import LiteralString, cast

from pydantic import ValidationError
from pydantic_core import ErrorDetails, InitErrorDetails, PydanticCustomError

from pytest_httpchain.models.types import NO_TEMPLATE, validate_partial_template_str, validate_unquoted_partial_template_str

# The member tags pydantic gives the template branch of a union, last in an
# error's location, after the field's own: built from the validators' names,
# as pydantic builds them.
_TEMPLATE_BRANCHES = frozenset(f"function-after[{validator.__name__}(), str]" for validator in (validate_partial_template_str, validate_unquoted_partial_template_str))

# The types of error by which a union member refuses what a string says, not
# that it is a string: a validator's ValueError, and pydantic's checks of a
# URL's text. Any other (``dict_type``, ``list_type``, ``int_parsing``, ...)
# refuses the input's type, where the template branch's hint helps: a string
# there is no literal, and may have been meant as a template.
_VALUE_REFUSALS = frozenset({"value_error", "url_parsing", "url_scheme", "url_syntax_violation", "url_too_long"})

# Where pydantic puts the error of a dict's key: after the entry's location,
# as a union member's tag is after the entry's value's.
_KEY = "[key]"

# What pydantic writes before a `value_error`'s own message.
_VALUE_ERROR = "Value error, "


def _holds_no_template(error: ErrorDetails) -> bool:
    """True for a union's template branch refusing a string for holding no
    template (not for any other complaint: an empty ``{{ }}`` is one)."""
    loc = error["loc"]
    return bool(loc) and loc[-1] in _TEMPLATE_BRANCHES and error["type"] == "value_error" and error["msg"].removeprefix(_VALUE_ERROR).startswith(NO_TEMPLATE)


def _judges_the_value(error: ErrorDetails) -> bool:
    """True for a union member other than a template branch refusing a value
    of a type it takes, at the member itself: its location is the field's,
    then the member's tag."""
    loc = error["loc"]
    return bool(loc) and isinstance(loc[-1], str) and loc[-1] not in _TEMPLATE_BRANCHES and loc[-1] != _KEY and error["type"] in _VALUE_REFUSALS


def without_template_hints(errors: Iterable[ErrorDetails]) -> list[ErrorDetails]:
    """``errors`` (as `ValidationError.errors` lists them) less each template
    branch's "Must contain at least one template expression" where another
    branch of the same field refused the value itself (`_VALUE_REFUSALS`).

    A template branch's error is told apart by its location's last element,
    the branch's member tag: so the field is the location before it, and an
    error whose location is that field's and one more element is another
    member's, never a sibling field's (a sibling's location shares the
    field's parent, not the field). A dict entry's key error shares the
    entry's location too, and is not a member. Both errors are kept where the
    hint helps: a literal branch that refused the input's type, and a
    template branch with another complaint (an empty ``{{ }}``).
    """
    errors = list(errors)
    judged = {error["loc"][:-1] for error in errors if _judges_the_value(error)}
    return [error for error in errors if not (_holds_no_template(error) and error["loc"][:-1] in judged)]


def validation_error(title: str, errors: Iterable[ErrorDetails], *, hide_input: bool = False) -> ValidationError:
    """``errors``, as `ValidationError.errors` lists them, as one
    `ValidationError` titled ``title``: the same errors, in the same order,
    each of its type, location, message and input. One of pydantic's own (it
    has a ``url``) is rebuilt as that type with its context, which writes its
    message, the link included, as pydantic did; a custom one, whose template
    is not kept, as a custom error of the same type and message.
    ``hide_input`` keeps each input out of the text, as the
    ``hide_input_in_errors`` of the model that raised them does."""
    details: list[InitErrorDetails] = []
    for error in errors:
        detail: InitErrorDetails = {"type": error["type"], "loc": error["loc"], "input": error["input"]}
        if "url" not in error:
            detail["type"] = PydanticCustomError(cast(LiteralString, error["type"]), cast(LiteralString, error["msg"]))
        elif "ctx" in error:
            detail["ctx"] = error["ctx"]
        details.append(detail)
    return ValidationError.from_exception_data(title, details, hide_input=hide_input)


def reported(e: ValidationError, *, hide_input: bool = False) -> ValidationError:
    """``e`` as `without_template_hints` leaves it: ``e`` itself when no
    error goes, else rebuilt (`validation_error`) with ``hide_input``, which
    the caller passes as the model that raised ``e`` hides its input."""
    errors = e.errors()
    kept = without_template_hints(errors)
    return e if len(kept) == len(errors) else validation_error(e.title, kept, hide_input=hide_input)
