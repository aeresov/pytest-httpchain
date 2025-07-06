import keyword
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


ValidPythonVariableName = Annotated[str, AfterValidator(validate_python_variable_name)]
JMESPathExpression = Annotated[str, AfterValidator(validate_jmespath_expression)]


class Stage(BaseModel):
    name: str = Field()
    data: Any = Field()
    save: dict[ValidPythonVariableName, JMESPathExpression] | None = Field(default=None)


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
            if stage.save:
                for var_name in stage.save.keys():
                    if var_name in fixture_names:
                        raise ValueError(f"Variable name '{var_name}' conflicts with fixture name")

        return self
