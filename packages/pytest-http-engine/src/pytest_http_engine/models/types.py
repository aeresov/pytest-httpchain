import json
import keyword
import re
import xml.etree.ElementTree
from pathlib import Path
from typing import Annotated, Any

import jmespath
from pydantic import AfterValidator, PlainSerializer

from pytest_http_engine.user_function import UserFunction


def validate_python_identifier(v: str) -> str:
    if not v.isidentifier():
        raise ValueError(f"Ivalid Python variable name: '{v}'")

    if keyword.iskeyword(v) or (hasattr(keyword, "softkwlist") and v in keyword.softkwlist):
        raise ValueError(f"Python keyword is used as variable name: '{v}'")

    return v


def validate_jmespath_expression(v: str) -> str:
    try:
        jmespath.compile(v)
    except Exception as e:
        raise ValueError("Invalid JMESPath expression") from e

    return v


def validate_json_schema_inline(v: dict[str, Any]) -> dict[str, Any]:
    """Validate inline JSON schema dictionary."""
    # Basic JSON schema validation - check if it's a valid dict structure
    try:
        json.dumps(v)
    except (TypeError, ValueError) as e:
        raise ValueError("Invalid JSON schema format") from e

    # Optional: Basic schema structure validation
    if "$schema" in v and not isinstance(v["$schema"], str):
        raise ValueError("$schema must be a string")

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


VariableName = Annotated[str, AfterValidator(validate_python_identifier)]
FunctionName = Annotated[str, AfterValidator(UserFunction.validate_name)]
JMESPathExpression = Annotated[str, AfterValidator(validate_jmespath_expression)]
JSONSchemaInline = Annotated[dict[str, Any], AfterValidator(validate_json_schema_inline)]
SerializablePath = Annotated[Path, PlainSerializer(lambda x: str(x), return_type=str)]
RegexPattern = Annotated[str, AfterValidator(validate_regex_pattern)]
XMLSting = Annotated[str, AfterValidator(validate_xml)]
