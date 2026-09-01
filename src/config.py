from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_debug: bool = False
    session_secret: str = "development-only-change-me"
    internal_service_token: str = "development-internal-token"

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        if self.app_env == "production":
            if self.session_secret == "development-only-change-me":
                raise ValueError("SESSION_SECRET must be configured in production")
            if self.internal_service_token == "development-internal-token":
                raise ValueError("INTERNAL_SERVICE_TOKEN must be configured in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
