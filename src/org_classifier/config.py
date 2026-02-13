from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    cache_dir: str = ".cache"
    max_concurrent_requests: int = 5
    gemini_api_key: Optional[str] = None
    gemini_model: str = "gemini-2.0-flash"
    gemini_timeout_seconds: float = 30.0
    gemini_max_retries: int = 4
    gemini_backoff_base_seconds: float = 1.5
    gemini_backoff_max_seconds: float = 20.0
    gemini_min_interval_seconds: float = 1.2
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o-mini"
    openai_timeout_seconds: float = 30.0
    openai_max_retries: int = 3
    openai_backoff_base_seconds: float = 1.5
    openai_backoff_max_seconds: float = 20.0
    openai_min_interval_seconds: float = 1.2
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
