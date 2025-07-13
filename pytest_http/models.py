from http import HTTPMethod, HTTPStatus
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from pytest_http.types import FunctionName, JMESPathExpression, JSONSerializable, VariableName


class FunctionCall(BaseModel):
    function: FunctionName
    kwargs: dict[str, Any] | None = Field(default=None)


class Save(BaseModel):
    vars: dict[VariableName, JMESPathExpression] | None = Field(default=None)
    functions: list[FunctionName | FunctionCall] | None = Field(default=None)


class Verify(BaseModel):
    status: HTTPStatus | None = Field(default=None)
    json: dict[JMESPathExpression, Any] | None = Field(default=None)
    functions: list[FunctionName | FunctionCall] | None = Field(default=None)


class Request(BaseModel):
    url: str = Field()
    method: HTTPMethod = Field(default=HTTPMethod.GET)
    params: dict[str, Any] | None = Field(default=None)
    headers: dict[str, str] | None = Field(default=None)
    json: JSONSerializable = Field(default=None)


class Response(BaseModel):
    save: Save | None = Field(default=None)
    verify: Verify | None = Field(default=None)


class Stage(BaseModel):
    request: Request = Field()
    response: Response | None = Field(default=None)


class Stages(RootModel):
    root: dict[str, Stage] = Field(default_factory=dict)

    def __iter__(self):
        return iter(self.root)

    def __getitem__(self, item):
        return self.root[item]

    def items(self):
        return self.root.items()


class Scenario(BaseModel):
    fixtures: list[str] = Field(default_factory=list)
    marks: list[str] = Field(default_factory=list)
    stages: Stages = Field(default_factory=Stages)
    final: Stages = Field(default_factory=Stages)

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="after")
    def validate_save_variables_not_fixtures(self) -> "Scenario":
        if not self.fixtures:
            return self

        fixture_names = set(self.fixtures)

        for stage in self.stages:
            if stage.response and stage.response.save and stage.response.save.vars:
                for var_name in stage.response.save.vars.keys():
                    if var_name in fixture_names:
                        raise ValueError(f"Variable name '{var_name}' conflicts with fixture name")

        return self
