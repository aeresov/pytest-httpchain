import keyword
import re
import xml.etree.ElementTree
from pathlib import Path
from typing import Annotated, Any

import jmespath
import jsonschema
from pydantic import AfterValidator, PlainSerializer
from pytest_httpchain_templates.expressions import TEMPLATE_PATTERN, is_complete_template
from pytest_httpchain_userfunc.base import UserFunctionHandler
from pytest_httpchain_userfunc.exceptions import UserFunctionError


def create_string_validator(validation_func, error_message: str):
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

    if keyword.iskeyword(v) or (hasattr(keyword, "softkwlist") and v in keyword.softkwlist):
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


def check_json_schema(schema: dict[str, Any]) -> None:
    """Check JSON schema validity using appropriate validator version."""
    schema_uri = schema.get("$schema", "http://json-schema.org/draft-07/schema#")

    # Find matching validator
    validator_class = jsonschema.Draft7Validator  # Default
    for version_key, validator in SCHEMA_VALIDATORS.items():
        if version_key in schema_uri:
            validator_class = validator
            break

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
        raise ValueError(f"JSON Schema validation error: {str(e)}") from e

    return v


# Use the validator factory for simple validation cases
validate_jmespath_expression = create_string_validator(jmespath.compile, "Invalid JMESPath expression")

validate_regex_pattern = create_string_validator(re.compile, "Invalid regular expression")

validate_xml = create_string_validator(xml.etree.ElementTree.fromstring, "Invalid XML")


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
    try:
        # Just validate the name format using the regex pattern
        if not UserFunctionHandler.NAME_PATTERN.match(v):
            raise ValueError(f"Invalid function name format: {v}")
    except (AttributeError, UserFunctionError) as e:
        raise ValueError("Invalid user function") from e
    return v


# Type aliases with validators
VariableName = Annotated[str, AfterValidator(validate_python_identifier)]
FunctionImportName = Annotated[str, AfterValidator(validate_function_import_name)]
JMESPathExpression = Annotated[str, AfterValidator(validate_jmespath_expression)]
JSONSchemaInline = Annotated[dict[str, Any], AfterValidator(validate_json_schema_inline)]
SerializablePath = Annotated[Path, PlainSerializer(lambda x: str(x), return_type=str)]
RegexPattern = Annotated[str, AfterValidator(validate_regex_pattern)]
XMLSting = Annotated[str, AfterValidator(validate_xml)]
TemplateExpression = Annotated[str, AfterValidator(validate_template_expression)]
PartialTemplateStr = Annotated[str, AfterValidator(validate_partial_template_str)]
