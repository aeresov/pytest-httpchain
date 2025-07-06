import keyword
from typing import Annotated, Any

import jmespath
from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def validate_save_field(v: dict[str, str] | None) -> dict[str, str] | None:
    """Validate save field dictionary keys and values."""
    if v is None:
        return v

    for key, value in v.items():
        # Validate that key is a valid Python variable name
        if not key.isidentifier():
            raise ValueError(f"Key '{key}' is not a valid Python variable name")

        # Validate that key is not a Python keyword (hard keywords)
        if keyword.iskeyword(key):
            raise ValueError(f"Key '{key}' is a Python keyword and cannot be used as a variable name")

        # Validate that key is not a Python soft keyword (context-dependent keywords)
        if hasattr(keyword, "softkwlist") and key in keyword.softkwlist:
            raise ValueError(f"Key '{key}' is a Python keyword and cannot be used as a variable name")

        # Validate that value is a valid JMESPath expression
        try:
            jmespath.compile(value)
        except Exception as e:
            raise ValueError(f"Value '{value}' is not a valid JMESPath expression: {e}") from e

    return v


class Structure(BaseModel):
    fixtures: list[str] = Field(default_factory=list)
    marks: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


class Stage(BaseModel):
    name: str = Field()
    data: Any = Field()


class Scenario(BaseModel):
    stages: list[Stage] = Field(default_factory=list)
    save: Annotated[dict[str, str] | None, AfterValidator(validate_save_field)] = Field(default=None)

    model_config = ConfigDict(extra="ignore")
