"""Deterministic fake provider (offline default, also used by tests)."""

from app.schemas.llm import LLMMessage, LLMResponse
from app.services.llm.base import LLMProvider


PLACEHOLDER_RESPONSE = "JARVIS assistant core is online."


class FakeProvider(LLMProvider):
    """Always returns the fixed placeholder text (no network)."""

    def generate_response(self, messages: list[LLMMessage]) -> LLMResponse:
        _ = messages  # Accepted for interface parity; content is fixed.
        return LLMResponse(content=PLACEHOLDER_RESPONSE, model="fake")
