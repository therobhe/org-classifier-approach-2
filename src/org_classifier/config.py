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
    gemini_use_url_context: bool = False
    gemini_url_context_urls: Optional[str] = None
    gemini_use_google_search_grounding: bool = False
    gemini_media_resolution: str = "low"
    gemini_thinking_level: str = "low"
    gemini_use_structured_output: bool = False
    # Optional override for a compact JSON Schema (as JSON string) to request from Gemini.
    gemini_structured_output_schema: Optional[str] = None
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o-mini"
    openai_timeout_seconds: float = 30.0
    openai_max_retries: int = 3
    openai_backoff_base_seconds: float = 1.5
    openai_backoff_max_seconds: float = 20.0
    openai_min_interval_seconds: float = 1.2
    openai_tokens_per_minute: Optional[int] = None
    openai_requests_per_minute: Optional[int] = None
    openai_requests_per_day: Optional[int] = None
    openai_tokens_per_day: Optional[int] = None
    openai_quota_safety_margin: float = 0.9
    openai_wait_for_capacity_window: bool = True
    openai_max_completion_tokens: int = 120
    web_batch_size: int = 50
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
