"""What a stage's ``verify`` and ``save`` steps mean.

Pure functions over ``(resolved model, response)`` — no chain state — raising
`VerificationError` / `SaveError` on failure. The carrier owns the sequence.
"""

import json
import re
from collections import ChainMap
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
import jmespath
import jmespath.exceptions
import jsonschema
import referencing.exceptions

from pytest_httpchain.errors import SaveError, SchemaFileError, VerificationError
from pytest_httpchain.models import (
    HeaderMatcher,
    JMESPathSave,
    Save,
    SubstitutionsSave,
    UserFunctionsSave,
    Verify,
    check_json_schema,
    json_schema_validator_class,
)
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
                step_saved.update(func_result)  # ty: ignore[no-matching-overload]

        case _:
            raise RuntimeError(f"Unhandled save type: {type(save_model).__name__}")

    return step_saved


def check_rendered_assertions(declared: Verify, rendered: Verify) -> None:
    """Raise when an assertion the scenario declared rendered away to nothing.

    ``status`` and ``body.schema`` are optional, so a template resolving to
    ``None`` re-validates cleanly and `process_verify` would simply not run that
    check — turning an upstream mistake (a JMESPath save of a missing key,
    ``get()`` without a default) into a green stage. "Never declared" and
    "declared but rendered to nothing" must never look the same to a test runner.
    """
    for field, declared_value, rendered_value in (
        ("status", declared.status, rendered.status),
        ("body.schema", declared.body.schema, rendered.body.schema),
    ):
        if declared_value is not None and rendered_value is None:
            raise VerificationError(f"Verify '{field}' was declared as {declared_value!r} but rendered to None, which would silently drop the assertion")


def process_verify(verify_model: Verify, response: httpx.Response, scenario_dir: Path | None = None) -> None:
    """Run one verify step's assertions, raising `VerificationError` on the first failure."""
    # `is not None`, not truthiness: a rendered-away assertion must not be
    # indistinguishable from an undeclared one (see `check_rendered_assertions`).
    if verify_model.status is not None and response.status_code != verify_model.status:
        raise VerificationError(f"Status code doesn't match: expected {verify_model.status}, got {response.status_code}")

    for header_name, expected_value in verify_model.headers.items():
        match expected_value:
            case HeaderMatcher():
                # An absent header behaves as an empty string, as bodies do.
                actual = response.headers.get(header_name) or ""
                verify_text_matchers(
                    f"Header '{header_name}' (value: {actual!r})",
                    actual,
                    contains=optional_as_list(expected_value.contains),
                    not_contains=optional_as_list(expected_value.not_contains),
                    matches=optional_as_list(expected_value.matches),
                    not_matches=optional_as_list(expected_value.not_matches),
                )
            case _:
                if response.headers.get(header_name) != expected_value:
                    raise VerificationError(f"Header '{header_name}' doesn't match: expected {expected_value}, got {response.headers.get(header_name)}")

    for i, expression in enumerate(verify_model.expressions):
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
        _verify_body_schema(verify_model.body.schema, response, scenario_dir)

    verify_text_matchers(
        "Body",
        response.text,
        contains=verify_model.body.contains,
        not_contains=verify_model.body.not_contains,
        matches=verify_model.body.matches,
        not_matches=verify_model.body.not_matches,
    )


def _verify_body_schema(schema: Any, response: httpx.Response, scenario_dir: Path | None) -> None:
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

    try:
        response_json = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise VerificationError(f"Cannot validate schema, response is not valid JSON: {e}") from e

    try:
        # Already meta-checked (inline at model validation, files just above), so
        # instantiate the dialect's validator instead of jsonschema.validate,
        # which would re-run check_schema on every stage.
        json_schema_validator_class(schema)(schema).validate(response_json)
    except jsonschema.ValidationError as e:
        raise VerificationError(f"Body schema validation failed: {e}") from e
    except jsonschema.SchemaError as e:
        raise VerificationError(f"Invalid body validation schema: {e}") from e
    except referencing.exceptions.Unresolvable as e:
        # An unresolvable $ref inside the schema itself must fail the stage
        # cleanly, not escape as a raw traceback past the abort machinery.
        raise VerificationError(f"Cannot resolve $ref in body schema: {e}") from e


def verify_text_matchers(
    subject: str,
    text: str,
    *,
    contains: Iterable[str],
    not_contains: Iterable[str],
    matches: Iterable[Any],
    not_matches: Iterable[Any],
) -> None:
    """The contains/matches semantics, shared by body and header checks
    (patterns use ``re.search``)."""
    for substring in contains:
        if substring not in text:
            raise VerificationError(f"{subject} doesn't contain '{substring}'")

    for substring in not_contains:
        if substring in text:
            raise VerificationError(f"{subject} contains '{substring}' while it shouldn't")

    for pattern in matches:
        if not re.search(pattern, text):
            raise VerificationError(f"{subject} doesn't match '{pattern}'")

    for pattern in not_matches:
        if re.search(pattern, text):
            raise VerificationError(f"{subject} matches '{pattern}' while it shouldn't")
