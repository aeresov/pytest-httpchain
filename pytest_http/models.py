import importlib
import json
import keyword
from http import HTTPMethod, HTTPStatus
from typing import Annotated, Any

import jmespath
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator


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


def validate_json_serializable(v: Any) -> Any:
    """Validate that the value can be serialized as JSON."""
    if v is None:
        return v

    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError) as e:
        raise ValueError(f"Value cannot be serialized as JSON: {e}") from e


def validate_python_function_name(v: str) -> str:
    # Require module:function syntax
    if ":" not in v:
        raise ValueError(f"'{v}' must use 'module:function' syntax")

    module_path, function_name = v.rsplit(":", 1)

    if not module_path:
        raise ValueError(f"'{v}' is missing module path")

    if not function_name:
        raise ValueError(f"'{v}' is missing function name")

    # Actually verify the function exists and is callable
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ValueError(f"Cannot import module '{module_path}': {e}") from e

    if not hasattr(module, function_name):
        raise ValueError(f"Function '{function_name}' not found in module '{module_path}'")

    func = getattr(module, function_name)
    if not callable(func):
        raise ValueError(f"'{function_name}' in module '{module_path}' is not callable")

    return v


ValidPythonVariableName = Annotated[str, AfterValidator(validate_python_variable_name)]
ValidPythonFunctionName = Annotated[str, AfterValidator(validate_python_function_name)]
JMESPathExpression = Annotated[str, AfterValidator(validate_jmespath_expression)]
JSONSerializable = Annotated[Any, AfterValidator(validate_json_serializable)]


class FunctionCall(BaseModel):
    function: ValidPythonFunctionName
    kwargs: dict[str, Any] | None = Field(default=None)


class SaveConfig(BaseModel):
    vars: dict[ValidPythonVariableName, JMESPathExpression] | None = Field(default=None)
    functions: list[ValidPythonFunctionName | FunctionCall] | None = Field(default=None)


class Verify(BaseModel):
    status: HTTPStatus | None = Field(default=None)
    json_data: dict[JMESPathExpression, Any] | None = Field(default=None, alias="json")
    functions: list[ValidPythonFunctionName | FunctionCall] | None = Field(default=None)


class Request(BaseModel):
    url: str | None = Field(default=None)
    method: HTTPMethod = Field(default=HTTPMethod.GET)
    params: dict[str, Any] | None = Field(default=None)
    headers: dict[str, str] | None = Field(default=None)
    json: JSONSerializable = Field(default=None)


class Response(BaseModel):
    save: SaveConfig | None = Field(default=None)
    verify: Verify | None = Field(default=None)


class Stage(BaseModel):
    name: str = Field()
    request: Request = Field()
    response: Response | None = Field(default=None)


class Scenario(BaseModel):
    fixtures: list[str] = Field(default_factory=list)
    marks: list[str] = Field(default_factory=list)
    stages: list[Stage] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")

    @field_validator("fixtures", "marks", "stages", mode="before")
    @classmethod
    def convert_none_to_empty_list(cls, v):
        if v is None:
            return []
        return v

    @model_validator(mode="after")
    def validate_save_variables_not_fixtures(self) -> "Scenario":
        if not self.fixtures:
            return self

        fixture_names = set(self.fixtures)

        for stage in self.stages:
            if stage.save and stage.save.vars:
                for var_name in stage.save.vars.keys():
                    if var_name in fixture_names:
                        raise ValueError(f"Variable name '{var_name}' conflicts with fixture name")

        return self
