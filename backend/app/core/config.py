"""Application configuration using Pydantic Settings."""

import logging
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_name: str = Field(default="JARVIS", description="Application name")
    app_version: str = Field(default="0.1.0", description="Application version")
    app_env: str = Field(default="development", description="Application environment")

    # Server
    host: str = Field(default="127.0.0.1", description="Server host")
    port: int = Field(default=8000, description="Server port")

    # Logging
    log_level: str = Field(default="INFO", description="Logging level")
    log_format: str = Field(default="json", description="Log format (json or text)")

    # LLM provider (fake = deterministic offline default, gemini = live API)
    llm_provider: str = Field(default="fake", description="LLM provider name")
    gemini_model: str = Field(
        default="gemini-2.0-flash", description="Gemini model identifier"
    )
    gemini_api_key: Optional[str] = Field(
        default=None, description="Gemini API key (never logged)"
    )

    @field_validator("app_env")
    @classmethod
    def validate_app_env(cls, v: str) -> str:
        """Validate application environment."""
        allowed = {"development", "staging", "production"}
        v_lower = v.lower()
        if v_lower not in allowed:
            raise ValueError(f"app_env must be one of {allowed}, got '{v}'")
        return v_lower

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate logging level."""
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v_upper = v.upper()
        if v_upper not in allowed:
            raise ValueError(f"log_level must be one of {allowed}, got '{v}'")
        return v_upper

    @field_validator("log_format")
    @classmethod
    def validate_log_format(cls, v: str) -> str:
        """Validate log format."""
        allowed = {"json", "text"}
        v_lower = v.lower()
        if v_lower not in allowed:
            raise ValueError(f"log_format must be one of {allowed}, got '{v}'")
        return v_lower

    @field_validator("llm_provider")
    @classmethod
    def validate_llm_provider(cls, v: str) -> str:
        """Validate LLM provider name."""
        allowed = {"fake", "gemini"}
        v_lower = v.lower()
        if v_lower not in allowed:
            raise ValueError(f"llm_provider must be one of {allowed}, got '{v}'")
        return v_lower


settings = Settings()