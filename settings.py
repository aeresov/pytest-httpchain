from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    suffix: str = Field(default="http")

    model_config = SettingsConfigDict()
