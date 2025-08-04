import keyword
import re
import xml.etree.ElementTree
from pathlib import Path
from typing import Annotated, Any

import jmespath
import jsonschema
from pydantic import AfterValidator, PlainSerializer

from pytest_httpchain_engine.template_pattern import TEMPLATE_REGEX, is_complete_template
from pytest_httpchain_engine.user_function import BaseUserFunction


def validate_python_identifier(v: str) -> str:
    if not v.isidentifier():
        raise ValueError(f"Invalid Python variable name: '{v}'")

    if keyword.iskeyword(v) or (hasattr(keyword, "softkwlist") and v in keyword.softkwlist):
        raise ValueError(f"Python keyword is used as variable name: '{v}'")

    return v


def validate_jmespath_expression(v: str) -> str:
    try:
        jmespath.compile(v)
    except Exception as e:
        raise ValueError("Invalid JMESPath expression") from e
    return v


def check_json_schema(schema: dict[str, Any]) -> None:
    """Check if a dictionary is a valid JSON Schema using the appropriate meta-schema.

    This function validates the schema structure itself (meta-schema validation),
    not validating data against a schema.

    Note: JSON Schema drafts are not fully backwards compatible. Major differences:
    - Draft 4: uses 'id' and boolean 'exclusiveMinimum'
    - Draft 6+: uses '$id' and numeric 'exclusiveMinimum'
    - Draft 2019-09: adds '$defs' alongside 'definitions'
    - Draft 2020-12: deprecates array form of 'items' (use 'prefixItems')

    Args:
        schema: The JSON Schema dictionary to validate

    Raises:
        jsonschema.SchemaError: If the schema is invalid
    """
    # Determine which validator to use based on $schema
    schema_uri = schema.get("$schema", "http://json-schema.org/draft-07/schema#")

    if "draft-03" in schema_uri or "draft-3" in schema_uri:
        jsonschema.Draft3Validator.check_schema(schema)
    elif "draft-04" in schema_uri or "draft-4" in schema_uri:
        jsonschema.Draft4Validator.check_schema(schema)
    elif "draft-06" in schema_uri or "draft-6" in schema_uri:
        jsonschema.Draft6Validator.check_schema(schema)
    elif "draft-07" in schema_uri or "draft-7" in schema_uri:
        jsonschema.Draft7Validator.check_schema(schema)
    elif "2019-09" in schema_uri:
        jsonschema.Draft201909Validator.check_schema(schema)
    elif "2020-12" in schema_uri:
        jsonschema.Draft202012Validator.check_schema(schema)
    else:
        # Default to Draft 7 for unknown versions
        jsonschema.Draft7Validator.check_schema(schema)


def validate_json_schema_inline(v: dict[str, Any]) -> dict[str, Any]:
    """Validate inline JSON schema dictionary using JSON Schema meta-schema.

    This is a Pydantic validator that wraps check_json_schema for use in models.
    """
    try:
        check_json_schema(v)
    except jsonschema.SchemaError as e:
        raise ValueError(f"Invalid JSON Schema: {e.message}") from e
    except Exception as e:
        raise ValueError(f"JSON Schema validation error: {str(e)}") from e

    return v


def validate_regex_pattern(v: str) -> str:
    """Validate that a string is a valid regular expression."""
    try:
        re.compile(v)
    except re.PatternError as e:
        raise ValueError("Invalid regular expression") from e
    return v


def validate_xml(v: str) -> str:
    try:
        xml.etree.ElementTree.fromstring(v)
    except xml.etree.ElementTree.ParseError as e:
        raise ValueError("Invalid XML") from e
    return v


def validate_template_expression(v: str) -> str:
    """Validate that a string is a complete template expression."""
    if not is_complete_template(v):
        raise ValueError(f"Must be a complete template expression like '{{{{ expr }}}}', got: {v!r}")
    return v


def validate_partial_template_str(v: str) -> str:
    """Validate that a string contains at least one template expression."""
    matches = list(TEMPLATE_REGEX.finditer(v))
    if not matches:
        raise ValueError(f"Must contain at least one template expression like '{{{{ expr }}}}', got: {v!r}")

    # Check that all template expressions are non-empty
    for match in matches:
        if not match.group("expr").strip():
            raise ValueError(f"Template expression cannot be empty at position {match.start()}")
    return v


VariableName = Annotated[str, AfterValidator(validate_python_identifier)]
FunctionImportName = Annotated[str, AfterValidator(BaseUserFunction.validate_name)]
JMESPathExpression = Annotated[str, AfterValidator(validate_jmespath_expression)]
JSONSchemaInline = Annotated[dict[str, Any], AfterValidator(validate_json_schema_inline)]
SerializablePath = Annotated[Path, PlainSerializer(lambda x: str(x), return_type=str)]
RegexPattern = Annotated[str, AfterValidator(validate_regex_pattern)]
XMLSting = Annotated[str, AfterValidator(validate_xml)]
TemplateExpression = Annotated[str, AfterValidator(validate_template_expression)]
PartialTemplateStr = Annotated[str, AfterValidator(validate_partial_template_str)]
