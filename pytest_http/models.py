from pydantic import BaseModel, ConfigDict, Field


class Structure(BaseModel):
    fixtures: list[str] = Field(default_factory=list)
    marks: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


class Stage(BaseModel):
    name: str = Field()
    data: int = Field()


class Test(BaseModel):
    stages: list[Stage] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")
