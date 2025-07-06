import keyword
from typing import Annotated, Any

import jmespath
from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def validate_python_variable_name(v: str) -> str:
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


ValidPythonVariableName = Annotated[str, AfterValidator(validate_python_variable_name)]
JMESPathExpression = Annotated[str, AfterValidator(validate_jmespath_expression)]


class Structure(BaseModel):
    fixtures: list[str] = Field(default_factory=list)
    marks: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


class Stage(BaseModel):
    name: str = Field()
    data: Any = Field()
    save: dict[ValidPythonVariableName, JMESPathExpression] | None = Field(default=None)


class Scenario(BaseModel):
    stages: list[Stage] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")
