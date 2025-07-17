import json
import keyword
from typing import Annotated, Any

import jmespath
from pydantic import AfterValidator

from pytest_http_engine.user_function import UserFunction


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


VariableName = Annotated[str, AfterValidator(validate_python_identifier)]
FunctionName = Annotated[str, AfterValidator(UserFunction.validate_name)]
JMESPathExpression = Annotated[str, AfterValidator(validate_jmespath_expression)]
JSONSerializable = Annotated[Any, AfterValidator(validate_json_serializable)]
