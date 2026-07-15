"""Annotated string/dict type aliases (with validators) for the scenario models.

Most aliases are ``Annotated[str, AfterValidator(...)]`` that validate a field's
contents (JMESPath, regex, XML, GraphQL, base64, a complete vs. partial template,
a function import name, a Python-identifier variable name, an inline JSON Schema,
a serializable path).

Two aliases handle a ``SimpleNamespace``<->``dict`` round-trip: user-supplied
``vars`` become attribute-accessible inside ``{{ }}`` templates (dict ->
SimpleNamespace), and values headed back into a request body are normalized to
plain dicts so they stay JSON-serializable (SimpleNamespace -> dict).
"""

import base64
import keyword
import logging
import re
import types
import xml.etree.ElementTree
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import graphql
import jmespath
import jsonschema
from pydantic import AfterValidator, BeforeValidator, Field, JsonValue, PlainSerializer, WithJsonSchema

from pytest_httpchain.templates import TEMPLATE_PATTERN, is_complete_template
from pytest_httpchain.userfunc import NAME_PATTERN


def create_string_validator(validation_func: Callable[[str], Any], error_message: str) -> Callable[[str], str]:
    """Factory for creating string validators."""

    def validator(v: str) -> str:
        try:
            validation_func(v)
        except Exception as e:
            raise ValueError(error_message) from e
        return v

    return validator


def validate_python_identifier(v: str) -> str:
    """Validate Python identifier and check for reserved keywords."""
    if not v.isidentifier():
        raise ValueError(f"Invalid Python variable name: '{v}'")

    if keyword.iskeyword(v) or v in keyword.softkwlist:
        raise ValueError(f"Python keyword is used as variable name: '{v}'")

    return v


# Map schema versions to validators
SCHEMA_VALIDATORS = {
    "draft-03": jsonschema.Draft3Validator,
    "draft-3": jsonschema.Draft3Validator,
    "draft-04": jsonschema.Draft4Validator,
    "draft-4": jsonschema.Draft4Validator,
    "draft-06": jsonschema.Draft6Validator,
    "draft-6": jsonschema.Draft6Validator,
    "draft-07": jsonschema.Draft7Validator,
    "draft-7": jsonschema.Draft7Validator,
    "2019-09": jsonschema.Draft201909Validator,
    "2020-12": jsonschema.Draft202012Validator,
}


logger = logging.getLogger(__name__)


def check_json_schema(schema: dict[str, Any]) -> None:
    """Check JSON schema validity using appropriate validator version."""
    schema_uri = schema.get("$schema", "http://json-schema.org/draft-07/schema#")

    # Find matching validator
    validator_class = jsonschema.Draft7Validator  # Default
    for version_key, validator in SCHEMA_VALIDATORS.items():
        if version_key in schema_uri:
            validator_class = validator
            break
    else:
        if "$schema" in schema:
            logger.warning(f"Unrecognized JSON Schema version '{schema_uri}', falling back to Draft 7")

    validator_class.check_schema(schema)


def validate_json_schema_inline(v: dict[str, Any]) -> dict[str, Any]:
    """Validate inline JSON schema dictionary using JSON Schema meta-schema.

    This is a Pydantic validator that wraps check_json_schema for use in models.
    """
    try:
        check_json_schema(v)
    except jsonschema.SchemaError as e:
        raise ValueError(f"Invalid JSON Schema: {e.message}") from e
    except Exception as e:
        raise ValueError(f"JSON Schema validation error: {e}") from e

    return v


# Use the validator factory for simple validation cases
validate_jmespath_expression = create_string_validator(jmespath.compile, "Invalid JMESPath expression")

validate_regex_pattern = create_string_validator(re.compile, "Invalid regular expression")

validate_xml = create_string_validator(xml.etree.ElementTree.fromstring, "Invalid XML")

validate_graphql_query = create_string_validator(graphql.parse, "Invalid GraphQL query")

validate_base64 = create_string_validator(lambda v: base64.b64decode(v, validate=True), "Invalid base64 encoding")


def validate_template_expression(v: str) -> str:
    if not is_complete_template(v):
        raise ValueError(f"Must be a complete template expression like '{{{{ expr }}}}', got: {v!r}")
    return v


def validate_partial_template_str(v: str) -> str:
    matches = list(re.finditer(TEMPLATE_PATTERN, v))
    if not matches:
        raise ValueError(f"Must contain at least one template expression like '{{{{ expr }}}}', got: {v!r}")

    for match in matches:
        if not match.group("expr").strip():
            raise ValueError(f"Template expression cannot be empty at position {match.start()}")
    return v


def validate_function_import_name(v: str) -> str:
    """Validate function import name format.

    Format: module.path:function_name — the module path is required, matching
    the grammar the importer accepts, so a bare name fails here (at
    validation/collection) instead of only at runtime import.
    """
    if not NAME_PATTERN.match(v):
        if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", v):
            raise ValueError(f"Module path is required: use 'module:{v}' format instead of '{v}'")
        raise ValueError(f"Invalid function name format: {v}")
    return v


def convert_dict_to_namespace(v: Any) -> Any:
    """Recursively turn dicts into ``SimpleNamespace`` so ``{{ var.attr }}`` attribute
    access works in templates (used by ``VarsSubstitution.vars`` via ``NamespaceFromDict``)."""
    if isinstance(v, dict):
        converted = {}
        for key, value in v.items():
            converted[key] = convert_dict_to_namespace(value)
        return types.SimpleNamespace(**converted)
    elif isinstance(v, list):
        return [convert_dict_to_namespace(item) for item in v]
    else:
        return v


def convert_namespace_to_dict(v: Any) -> Any:
    """Recursively normalize any ``SimpleNamespace`` back to a plain dict so the value
    is JSON-serializable (used by ``JsonBody.json`` and GraphQL variables via ``NamespaceOrDict``)."""
    if isinstance(v, types.SimpleNamespace):
        result = {}
        for key, value in vars(v).items():
            result[key] = convert_namespace_to_dict(value)
        return result
    elif isinstance(v, list):
        return [convert_namespace_to_dict(item) for item in v]
    elif isinstance(v, dict):
        return {key: convert_namespace_to_dict(value) for key, value in v.items()}
    else:
        return v


# Type aliases with validators
VariableName = Annotated[str, AfterValidator(validate_python_identifier)]
FunctionImportName = Annotated[str, AfterValidator(validate_function_import_name)]
JMESPathExpression = Annotated[str, AfterValidator(validate_jmespath_expression)]
JSONSchemaInline = Annotated[dict[str, Any], AfterValidator(validate_json_schema_inline)]
SerializablePath = Annotated[Path, PlainSerializer(lambda x: str(x), return_type=str)]
RegexPattern = Annotated[str, AfterValidator(validate_regex_pattern)]
XMLString = Annotated[str, AfterValidator(validate_xml)]
GraphQLQuery = Annotated[str, AfterValidator(validate_graphql_query)]
TemplateExpression = Annotated[str, AfterValidator(validate_template_expression)]
PartialTemplateStr = Annotated[str, AfterValidator(validate_partial_template_str)]

# JSON-schema patterns for the published editor schema. Runtime validation is
# unchanged (still `validate_template_expression`); these only tighten the
# `string` branch the schema emits for `concrete | template` fields, so an
# editor flags a non-template string that is also not a valid value for the
# concrete type (e.g. timeout "abc"), without rejecting templates, concrete
# values, or the stringified concretes the runtime coerces.
_COMPLETE_TEMPLATE_PATTERN = rf"^\s*{TEMPLATE_PATTERN}\s*$"
_NUMBER_OR_TEMPLATE_PATTERN = rf"(?:{_COMPLETE_TEMPLATE_PATTERN})|(?:^[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)$)"

# Whole-value template where the concrete type's valid strings are already
# covered by the union's other (enum/bool) branch — so the string branch may be
# template-only (used for `method`, `allow_redirects`, `always_run`).
TemplateExpressionOnly = Annotated[
    str,
    AfterValidator(validate_template_expression),
    WithJsonSchema({"type": "string", "pattern": _COMPLETE_TEMPLATE_PATTERN}),
]
# Whole-value template for a numeric concrete type, whose stringified form the
# runtime coerces (e.g. "30" -> 30.0, "200" -> 200) — so the string branch
# accepts a template OR a numeric literal (used for timeout, status, repeat,
# max_concurrency, calls_per_sec, max_rate_limit_delay).
NumberOrTemplate = Annotated[
    str,
    AfterValidator(validate_template_expression),
    WithJsonSchema({"type": "string", "pattern": _NUMBER_OR_TEMPLATE_PATTERN}),
]

# Any RFC 9110 token is a legal HTTP method (httpx sends arbitrary methods), so
# non-enum verbs — WebDAV's PROPFIND/REPORT, cache PURGE, vendor methods — are
# representable. Sits AFTER the stdlib ``HTTPMethod`` branch in unions so the
# common verbs still normalize to the enum (and editors keep its autocomplete).
_HTTP_METHOD_TOKEN_PATTERN = r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$"


def validate_http_method_token(v: str) -> str:
    """Validate an HTTP method as an RFC 9110 token."""
    if not re.fullmatch(_HTTP_METHOD_TOKEN_PATTERN, v):
        raise ValueError(f"Invalid HTTP method token: {v!r}")
    return v


HttpMethodToken = Annotated[
    str,
    AfterValidator(validate_http_method_token),
    WithJsonSchema({"type": "string", "pattern": _HTTP_METHOD_TOKEN_PATTERN}),
]

# Any int in the registered HTTP status range, so nonstandard codes (nginx 499,
# 599, vendor codes) can be asserted. Sits AFTER the stdlib ``HTTPStatus``
# branch in unions so standard codes still normalize to the enum.
StatusCode = Annotated[int, Field(ge=100, le=599)]

Base64String = Annotated[str, AfterValidator(validate_base64)]
NamespaceFromDict = Annotated[Any, AfterValidator(convert_dict_to_namespace)]
# NamespaceOrDict ACCEPTS a SimpleNamespace or a dict on input and always yields a dict.
NamespaceOrDict = Annotated[dict[str, JsonValue], BeforeValidator(convert_namespace_to_dict)]
