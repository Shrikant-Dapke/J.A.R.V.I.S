"""Application configuration using Pydantic Settings."""

import ipaddress
import re
from pathlib import PureWindowsPath
from typing import Optional
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Ollama local provider bounds (shared with the provider implementation).
OLLAMA_MODEL_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}(?::[A-Za-z0-9._-]{1,120})?$"
)
OLLAMA_TIMEOUT_BOUNDS = (1.0, 600.0)
OLLAMA_NUM_CTX_BOUNDS = (256, 32768)


def validate_loopback_http_url(url: str) -> str:
    """
    Require an http(s) base URL that can only ever target the loopback
    interface (127.0.0.0/8, ::1, or localhost).

    Used for the local model runtime so prompts cannot be silently
    redirected to arbitrary remote hosts.
    """
    value = (url or "").strip()
    if not value:
        raise ValueError("expected a non-empty http(s) URL")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("expected the http or https scheme")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("must not embed credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("must not contain a query or fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("must be a bare base URL without a path")
    host = parsed.hostname or ""
    is_loopback = False
    if host == "localhost":
        is_loopback = True
    else:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise ValueError(
            "must target the loopback interface "
            "(e.g. http://127.0.0.1:11434)"
        )
    return value


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

    # LLM provider (fake = deterministic offline default, gemini = live API,
    # ollama = local model via the loopback Ollama runtime)
    llm_provider: str = Field(default="fake", description="LLM provider name")
    gemini_model: str = Field(
        default="gemini-2.0-flash", description="Gemini model identifier"
    )
    gemini_api_key: Optional[str] = Field(
        default=None, description="Gemini API key (never logged)"
    )

    # Ollama local provider (Phase 1 — text generation only). The base URL
    # is restricted to the loopback interface so prompts never leave the
    # machine unknowingly, and no secrets are involved.
    ollama_base_url: str = Field(
        default="http://127.0.0.1:11434",
        description="Loopback base URL of the local Ollama runtime",
    )
    ollama_model: str = Field(
        default="qwen3.5:4b-q4_K_M",
        description=(
            "Ollama chat model identifier (Phase 1: qwen3.5:4b-q4_K_M)"
        ),
    )
    ollama_timeout_seconds: float = Field(
        default=120.0,
        description="Bounded Ollama request timeout in seconds",
    )
    ollama_num_ctx: int = Field(
        default=4096,
        description="Ollama context window size in tokens",
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
        allowed = {"fake", "gemini", "ollama"}
        v_lower = v.lower()
        if v_lower not in allowed:
            raise ValueError(f"llm_provider must be one of {allowed}, got '{v}'")
        return v_lower

    @field_validator("ollama_base_url")
    @classmethod
    def validate_ollama_base_url(cls, v: str) -> str:
        """Restrict the Ollama runtime to a loopback HTTP endpoint."""
        try:
            return validate_loopback_http_url(v)
        except ValueError as exc:
            raise ValueError(f"ollama_base_url {exc}") from exc

    @field_validator("ollama_model")
    @classmethod
    def validate_ollama_model(cls, v: str) -> str:
        """Validate the Ollama model identifier format."""
        value = (v or "").strip()
        if not OLLAMA_MODEL_NAME_RE.match(value):
            raise ValueError(
                "ollama_model must be a valid model identifier "
                "(e.g. qwen3.5:9b)"
            )
        return value

    @field_validator("ollama_timeout_seconds")
    @classmethod
    def validate_ollama_timeout_seconds(cls, v: float) -> float:
        """Accept only bounded request timeouts."""
        low, high = OLLAMA_TIMEOUT_BOUNDS
        timeout = float(v)
        if not low <= timeout <= high:
            raise ValueError(
                f"ollama_timeout_seconds must be between {low} and {high}"
            )
        return timeout

    @field_validator("ollama_num_ctx")
    @classmethod
    def validate_ollama_num_ctx(cls, v: int) -> int:
        """Accept only bounded context window sizes."""
        low, high = OLLAMA_NUM_CTX_BOUNDS
        if not low <= v <= high:
            raise ValueError(
                f"ollama_num_ctx must be between {low} and {high}"
            )
        return v


settings = Settings()
