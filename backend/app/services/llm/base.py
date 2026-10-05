"""LLM provider interface (text in, text out, no SDK leakage)."""

from abc import ABC, abstractmethod

from app.schemas.llm import LLMMessage, LLMResponse


class LLMProvider(ABC):
    """Abstract text-generation provider."""

    @abstractmethod
    def generate_response(self, messages: list[LLMMessage]) -> LLMResponse:
        """
        Generate an assistant reply for the given conversation messages.

        Raises:
            LLMError: On configuration, provider, timeout, or
                invalid-response failures.
        """
        raise NotImplementedError
