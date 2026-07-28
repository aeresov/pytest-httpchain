"""Validated type aliases for the scenario models: content validators (JMESPath,
regex, XML, GraphQL, base64, templates, import names, identifiers, schemas,
paths) and the ``SimpleNamespace``<->``dict`` round-trip that makes ``vars``
attribute-accessible in templates and JSON-serializable in bodies."""

import base64
import keyword
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

from pytest_httpchain.constants import USER_FUNCTION_NAME_PATTERN
from pytest_httpchain.templates import TEMPLATE_PATTERN, TEMPLATE_PATTERN_ECMA, is_complete_template


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


def json_schema_validator_class(schema: dict[str, Any]) -> type[jsonschema.protocols.Validator]:
    """The validator class for a schema's declared dialect.

    Draft 2020-12 is pinned as the fallback — the dialect
    ``jsonschema.validate`` would pick — so meta-checking and instance
    validation always agree.
    """
    return jsonschema.validators.validator_for(schema, default=jsonschema.Draft202012Validator)


def check_json_schema(schema: dict[str, Any]) -> None:
    """Check JSON schema validity against its declared dialect's meta-schema."""
    json_schema_validator_class(schema).check_schema(schema)


def validate_json_schema_inline(v: dict[str, Any]) -> dict[str, Any]:
    """`check_json_schema` as a pydantic validator."""
    try:
        check_json_schema(v)
    except jsonschema.SchemaError as e:
        raise ValueError(f"Invalid JSON Schema: {e.message}") from e
    except Exception as e:
        raise ValueError(f"JSON Schema validation error: {e}") from e

    return v


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
    """Validate a ``module.path:function_name`` against the grammar the importer
    accepts, so a bare name fails here rather than at runtime import."""
    if not USER_FUNCTION_NAME_PATTERN.match(v):
        if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", v):
            raise ValueError(f"Module path is required: use 'module:{v}' format instead of '{v}'")
        raise ValueError(f"Invalid function name format: {v}")
    return v


def convert_dict_to_namespace(v: Any) -> Any:
    """Recursively turn dicts into ``SimpleNamespace``, so ``{{ var.attr }}``
    works in templates."""
    match v:
        case dict():
            return types.SimpleNamespace(**{key: convert_dict_to_namespace(value) for key, value in v.items()})
        case list():
            return [convert_dict_to_namespace(item) for item in v]
        case _:
            return v


def convert_namespace_to_dict(v: Any) -> Any:
    """Recursively normalize ``SimpleNamespace`` back to dicts, so the value is
    JSON-serializable."""
    match v:
        case types.SimpleNamespace():
            return {key: convert_namespace_to_dict(value) for key, value in vars(v).items()}
        case list():
            return [convert_namespace_to_dict(item) for item in v]
        case dict():
            return {key: convert_namespace_to_dict(value) for key, value in v.items()}
        case _:
            return v


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

# Editor-schema only: these tighten the `string` branch of `concrete | template`
# fields so an editor flags e.g. timeout "abc", without affecting runtime
# validation. ECMA-262 spelling, since JSON Schema `pattern` is a JS regex.
_COMPLETE_TEMPLATE_PATTERN = rf"^\s*{TEMPLATE_PATTERN_ECMA}\s*$"
_NUMBER_OR_TEMPLATE_PATTERN = rf"(?:{_COMPLETE_TEMPLATE_PATTERN})|(?:^[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)$)"

# For fields whose concrete strings are covered by an enum/bool branch already.
TemplateExpressionOnly = Annotated[
    str,
    AfterValidator(validate_template_expression),
    WithJsonSchema({"type": "string", "pattern": _COMPLETE_TEMPLATE_PATTERN}),
]
# For numeric fields, whose stringified form the runtime coerces ("30" -> 30.0).
NumberOrTemplate = Annotated[
    str,
    AfterValidator(validate_template_expression),
    WithJsonSchema({"type": "string", "pattern": _NUMBER_OR_TEMPLATE_PATTERN}),
]

# Any RFC 9110 token is a legal method (PROPFIND, PURGE, vendor verbs). Sits
# after the ``HTTPMethod`` branch so common verbs still normalize to the enum.
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

# Nonstandard codes (nginx 499) must be assertable. Sits after ``HTTPStatus``.
StatusCode = Annotated[int, Field(ge=100, le=599)]

Base64String = Annotated[str, AfterValidator(validate_base64)]
NamespaceFromDict = Annotated[Any, AfterValidator(convert_dict_to_namespace)]
# Accepts a SimpleNamespace or a dict; always yields a dict.
NamespaceOrDict = Annotated[dict[str, JsonValue], BeforeValidator(convert_namespace_to_dict)]
