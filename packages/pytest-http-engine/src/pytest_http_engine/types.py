import json
import keyword
import re
from pathlib import Path
from typing import Annotated, Any

import jmespath
from pydantic import AfterValidator, PlainSerializer
from pydantic_core import core_schema

from pytest_http_engine.user_function import UserFunction

# Common regex pattern for file references
# Ensures path is not empty and contains non-whitespace characters
FILE_REF_PATTERN = re.compile(r"^@(?P<path>\S.*?)$")


class FilePath(str):
    """String subclass for file paths prefixed with '@'.

    This allows isinstance() checks to distinguish between regular strings
    and file path references in pydantic models.
    """

    def __new__(cls, value: str) -> "FilePath":
        """Create a new FilePath instance from a string value."""
        if not isinstance(value, str):
            raise TypeError(f"FilePath must be constructed from a string, got {type(value)}")
        return super().__new__(cls, value)

    @property
    def path(self) -> Path:
        """Extract the path portion after the '@' prefix as a Path object.

        Returns:
            The file path without the '@' prefix as a pathlib.Path object.

        Note:
            Validation has already been performed during model validation,
            so this property can safely assume the format is correct.
        """
        match = FILE_REF_PATTERN.match(self)
        # This should never fail since validation happened during model creation
        assert match is not None, f"FilePath validation should have caught this: {self}"
        return Path(match.group("path"))

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler) -> core_schema.CoreSchema:
        """Generate pydantic core schema for FilePath."""
        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())


def validate_python_identifier(v: str) -> str:
    if not v.isidentifier():
        raise ValueError(f"'{v}' is not a valid Python variable name")

    if keyword.iskeyword(v):
        raise ValueError(f"'{v}' is a Python keyword and cannot be used as a variable name")

    if hasattr(keyword, "softkwlist") and v in keyword.softkwlist:
        raise ValueError(f"'{v}' is a Python keyword and cannot be used as a variable name")

    return v


def validate_jmespath_expression(v: str) -> str:
    try:
        jmespath.compile(v)
    except Exception as e:
        raise ValueError(f"'{v}' is not a valid JMESPath expression: {e}") from e

    return v


def validate_json_serializable(v: Any) -> Any:
    if v is None:
        return v

    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError) as e:
        raise ValueError(f"Value cannot be serialized as JSON: {e}") from e


def validate_file_path(v: str) -> FilePath:
    """Validate file path format - must start with @/path/to/file."""
    match = FILE_REF_PATTERN.match(v)
    if not match:
        raise ValueError(f"File path must start with '@' and contain a non-empty path, got: {v}")

    # Validate path format by attempting to create Path object
    try:
        Path(match.group("path"))
    except (ValueError, TypeError) as e:
        raise ValueError(f"Invalid file path format: {e}") from e

    return FilePath(v)


def validate_json_schema_inline(v: dict[str, Any]) -> dict[str, Any]:
    """Validate inline JSON schema dictionary."""
    # Basic JSON schema validation - check if it's a valid dict structure
    try:
        json.dumps(v)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Invalid JSON schema format: {e}") from e

    # Optional: Basic schema structure validation
    if "$schema" in v and not isinstance(v["$schema"], str):
        raise ValueError("$schema must be a string")

    return v


VariableName = Annotated[str, AfterValidator(validate_python_identifier)]
FunctionName = Annotated[str, AfterValidator(UserFunction.validate_name)]
JMESPathExpression = Annotated[str, AfterValidator(validate_jmespath_expression)]
JSONSerializable = Annotated[Any, AfterValidator(validate_json_serializable)]
FilePathRef = Annotated[FilePath, AfterValidator(validate_file_path)]
JSONSchemaInline = Annotated[dict[str, Any], AfterValidator(validate_json_schema_inline)]
SerializablePath = Annotated[Path, PlainSerializer(lambda x: str(x), return_type=str)]
