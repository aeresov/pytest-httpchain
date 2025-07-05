import re
from typing import Annotated

from pydantic import AfterValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def validate_suffix(v: str) -> str:
    if not re.match(r"^[a-zA-Z0-9_-]+$", v):
        raise ValueError("suffix must contain only alphanumeric characters, underscores, and hyphens")
    if len(v) > 32:
        raise ValueError("suffix must be 32 characters or less")
    return v


class Settings(BaseSettings):
    suffix: Annotated[str, AfterValidator(validate_suffix)] = Field(default="http")

    model_config = SettingsConfigDict()
