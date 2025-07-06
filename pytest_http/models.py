from typing import Any

import jmespath
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Structure(BaseModel):
    fixtures: list[str] = Field(default_factory=list)
    marks: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


class Stage(BaseModel):
    name: str = Field()
    data: Any = Field()


class Scenario(BaseModel):
    stages: list[Stage] = Field(default_factory=list)
    save: dict[str, str] | None = Field(default=None)

    model_config = ConfigDict(extra="ignore")

    @field_validator("save")
    @classmethod
    def validate_save(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return v

        for key, value in v.items():
            # Validate that key is a valid Python variable name
            if not key.isidentifier():
                raise ValueError(f"Key '{key}' is not a valid Python variable name")

            # Validate that value is a valid JMESPath expression
            try:
                jmespath.compile(value)
            except Exception as e:
                raise ValueError(f"Value '{value}' is not a valid JMESPath expression: {e}") from e

        return v
