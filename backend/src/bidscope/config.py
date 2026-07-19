from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BIDSCOPE_",
        env_file=".env",
        extra="ignore",
    )

    app_mode: Literal["demo", "development", "production", "test"] = "demo"
    database_url: str = "postgresql+asyncpg://bidscope:bidscope@localhost:5432/bidscope"
    checkpoint_database_url: str = "postgresql+psycopg://bidscope:bidscope@localhost:5432/bidscope"
    real_model_enabled: bool = False
    admin_token: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
