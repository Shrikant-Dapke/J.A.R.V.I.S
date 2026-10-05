"""Provider selection (settings-driven, test-overridable)."""

from typing import Optional

from app.core.config import settings
from app.services.llm.base import LLMProvider
from app.services.llm.fake import FakeProvider
from app.services.llm.gemini import GeminiProvider


_override: Optional[LLMProvider] = None
_cached_gemini: Optional[GeminiProvider] = None


def get_llm_provider() -> LLMProvider:
    """
    Return the configured provider (fake by default, Gemini when selected).

    The Gemini client is cached so connections are reused.
    """
    global _cached_gemini
    if _override is not None:
        return _override
    if settings.llm_provider == "gemini":
        if _cached_gemini is None:
            _cached_gemini = GeminiProvider(
                api_key=settings.gemini_api_key,
                model=settings.gemini_model,
            )
        return _cached_gemini
    return FakeProvider()


def set_llm_provider_override(provider: Optional[LLMProvider]) -> None:
    """Pin a provider for tests (None clears the override)."""
    global _override
    _override = provider


def clear_llm_provider_cache() -> None:
    """Drop the cached Gemini client (tests and reconfiguration)."""
    global _cached_gemini
    _cached_gemini = None
