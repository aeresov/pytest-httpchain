import keyword
from http import HTTPStatus
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


def validate_python_function_name(v: str) -> str:
    # Support module:function syntax
    if ":" in v:
        module_path, function_name = v.rsplit(":", 1)
        
        # Validate module path (can contain dots)
        if not module_path:
            raise ValueError(f"'{v}' is not a valid Python function name - missing module path")
        
        # Module path parts should be valid identifiers
        for part in module_path.split("."):
            if not part.isidentifier():
                raise ValueError(f"'{v}' is not a valid Python function name - invalid module part '{part}'")
            if keyword.iskeyword(part):
                raise ValueError(f"'{v}' is not a valid Python function name - module part '{part}' is a keyword")
        
        # Validate function name part
        if not function_name.isidentifier():
            raise ValueError(f"'{v}' is not a valid Python function name - invalid function name '{function_name}'")
        
        if keyword.iskeyword(function_name):
            raise ValueError(f"'{v}' is a Python keyword and cannot be used as a function name")
        
        if hasattr(keyword, "softkwlist") and function_name in keyword.softkwlist:
            raise ValueError(f"'{v}' is a Python keyword and cannot be used as a function name")
    else:
        # Original validation for simple function names
        if not v.isidentifier():
            raise ValueError(f"'{v}' is not a valid Python function name")

        if keyword.iskeyword(v):
            raise ValueError(f"'{v}' is a Python keyword and cannot be used as a function name")

        if hasattr(keyword, "softkwlist") and v in keyword.softkwlist:
            raise ValueError(f"'{v}' is a Python keyword and cannot be used as a function name")

    return v


ValidPythonVariableName = Annotated[str, AfterValidator(validate_python_variable_name)]
ValidPythonFunctionName = Annotated[str, AfterValidator(validate_python_function_name)]
JMESPathExpression = Annotated[str, AfterValidator(validate_jmespath_expression)]


class SaveConfig(BaseModel):
    vars: dict[ValidPythonVariableName, JMESPathExpression] | None = Field(default=None)
    functions: list[ValidPythonFunctionName] | None = Field(default=None)


class Verify(BaseModel):
    status: HTTPStatus | None = Field(default=None)
    json_data: dict[JMESPathExpression, Any] | None = Field(default=None, alias="json")


class Stage(BaseModel):
    name: str = Field()
    url: str | None = Field(default=None)
    params: dict[str, Any] | None = Field(default=None)
    headers: dict[str, str] | None = Field(default=None)
    save: SaveConfig | None = Field(default=None)
    verify: Verify | None = Field(default=None)

    @field_validator("save", mode="before")
    @classmethod
    def normalize_save_field(cls, v):
        if v is None:
            return None
        
        # If it's already a SaveConfig or dict with vars/functions keys, leave it as is
        if isinstance(v, dict):
            # Check if this is the new format with vars/functions keys
            if "vars" in v or "functions" in v:
                return v
            # Otherwise, treat it as the old format (direct var mapping)
            else:
                return {"vars": v}
        
        return v


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
