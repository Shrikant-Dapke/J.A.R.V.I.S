"""Provider selection (settings-driven, test-overridable)."""

from typing import Optional

from app.core.config import settings
from app.services.llm.base import LLMProvider
from app.services.llm.fake import FakeProvider
from app.services.llm.gemini import GeminiProvider
from app.services.llm.ollama import OllamaProvider


_override: Optional[LLMProvider] = None
_cached_gemini: Optional[GeminiProvider] = None
_cached_ollama: Optional[OllamaProvider] = None


def get_llm_provider() -> LLMProvider:
    """
    Return the configured provider (fake by default, Gemini or local
    Ollama when selected).

    Clients that hold pooled connections are cached so they are reused
    across calls instead of being rebuilt per request.
    """
    global _cached_gemini, _cached_ollama
    if _override is not None:
        return _override
    if settings.llm_provider == "gemini":
        if _cached_gemini is None:
            _cached_gemini = GeminiProvider(
                api_key=settings.gemini_api_key,
                model=settings.gemini_model,
            )
        return _cached_gemini
    if settings.llm_provider == "ollama":
        if _cached_ollama is None:
            _cached_ollama = OllamaProvider(
                base_url=settings.ollama_base_url,
                model=settings.ollama_model,
                timeout_seconds=settings.ollama_timeout_seconds,
                num_ctx=settings.ollama_num_ctx,
            )
        return _cached_ollama
    return FakeProvider()


def set_llm_provider_override(provider: Optional[LLMProvider]) -> None:
    """Pin a provider for tests (None clears the override)."""
    global _override
    _override = provider


def clear_llm_provider_cache() -> None:
    """Drop the cached clients (tests and reconfiguration)."""
    global _cached_gemini, _cached_ollama
    _cached_gemini = None
    ollama, _cached_ollama = _cached_ollama, None
    if ollama is not None:
        try:
            ollama.close()
        except Exception:  # pragma: no cover - cache reset must never raise
            pass
