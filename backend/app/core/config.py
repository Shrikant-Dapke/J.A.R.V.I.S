"""Application configuration using Pydantic Settings."""

import ipaddress
from pathlib import PureWindowsPath
from typing import Optional

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # Application
    app_name: str = Field(default="JARVIS", description="Application name")
    app_version: str = Field(default="0.1.0", description="Application version")
    app_env: str = Field(default="development", description="Application environment")

    # Server
    host: str = Field(default="127.0.0.1", description="Server host")
    port: int = Field(default=8000, description="Server port")

    # Local API security. The token authenticates the API client; it is not
    # evidence that a user has reviewed or consented to an action.
    api_token: Optional[SecretStr] = Field(
        default=None,
        description="Bearer secret required by protected local API routes",
        validation_alias=AliasChoices("JARVIS_API_TOKEN", "API_TOKEN"),
    )
    allowed_origins: str = Field(
        default="",
        description="Comma-separated browser origins allowed to call the API",
        validation_alias=AliasChoices("JARVIS_ALLOWED_ORIGINS", "ALLOWED_ORIGINS"),
    )
    code_executable_path: Optional[str] = Field(
        default=None,
        description="Optional absolute path to the trusted VS Code executable",
        validation_alias=AliasChoices("JARVIS_CODE_EXECUTABLE", "CODE_EXECUTABLE_PATH"),
    )

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

    @field_validator("host")
    @classmethod
    def validate_loopback_host(cls, v: str) -> str:
        """Reject configurations that could bind the API beyond loopback."""
        value = v.strip()
        try:
            address = ipaddress.ip_address(value.strip("[]"))
        except ValueError as exc:
            raise ValueError(
                "host must be a literal loopback IP address (127.0.0.1 or ::1)"
            ) from exc
        if not address.is_loopback:
            raise ValueError("host must be a loopback IP address")
        return value

    @field_validator("api_token", mode="before")
    @classmethod
    def normalize_api_token(cls, v: Optional[str]) -> Optional[str]:
        """Treat an empty environment setting as unconfigured."""
        if v is None or not str(v).strip():
            return None
        return v

    @field_validator("api_token")
    @classmethod
    def validate_api_token(cls, v: Optional[SecretStr]) -> Optional[SecretStr]:
        """Require sufficient entropy length when authentication is enabled."""
        if v is not None and len(v.get_secret_value()) < 32:
            raise ValueError("api_token must contain at least 32 characters")
        return v

    @field_validator("code_executable_path")
    @classmethod
    def validate_code_executable_path(cls, v: Optional[str]) -> Optional[str]:
        """Require optional application paths to be absolute Windows paths."""
        if v is None or not v.strip():
            return None
        path = PureWindowsPath(v.strip())
        if not path.is_absolute():
            raise ValueError("code_executable_path must be an absolute path")
        return str(path)

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
