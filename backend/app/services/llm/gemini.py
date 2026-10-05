"""Gemini provider behind the LLM interface (text generation only).

The model NEVER receives tool access: this class converts messages to
text and returns text. It cannot invoke tools, run code, or touch the
filesystem. All model output must still be treated as untrusted text.
"""

from typing import Optional

from google import genai
from google.genai import errors as genai_errors

from app.core.logging import get_logger
from app.schemas.llm import LLMMessage, LLMResponse
from app.services.llm.base import LLMProvider
from app.services.llm.errors import (
    LLMConfigurationError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMTimeoutError,
)


logger = get_logger(__name__)

DEFAULT_TIMEOUT_MS = 30_000


def _classify_upstream_code(code: int | None) -> str:
    """Map an upstream HTTP code to a short error classification."""
    if code == 400:
        return "bad-request"
    if code in (401, 403):
        return "authentication-or-permission"
    if code == 404:
        return "not-found"
    if code == 429:
        return "quota-exceeded"
    if isinstance(code, int) and 500 <= code < 600:
        return "upstream-server-error"
    if isinstance(code, int):
        return "unexpected-upstream-status"
    return "unknown"


class GeminiProvider(LLMProvider):
    """LLMProvider backed by the Gemini API."""

    def __init__(
        self, api_key: Optional[str] = None, model: str = "gemini-2.0-flash"
    ) -> None:
        if not api_key or not api_key.strip():
            raise LLMConfigurationError("Gemini API key is not configured")
        self._model = model
        # Client is an attribute (not a local) so tests can inject a fake.
        self._client = genai.Client(
            api_key=api_key, http_options={"timeout": DEFAULT_TIMEOUT_MS}
        )

    def generate_response(self, messages: list[LLMMessage]) -> LLMResponse:
        contents = [
            {
                "role": "user" if m.role == "user" else "model",
                "parts": [{"text": m.content}],
            }
            for m in messages
        ]
        try:
            response = self._client.models.generate_content(
                model=self._model, contents=contents
            )
        except TimeoutError as exc:
            logger.warning(
                "Gemini request timed out",
                extra={"extra_fields": {"error_type": type(exc).__name__}},
            )
            raise LLMTimeoutError("Language model request timed out") from exc
        except genai_errors.APIError as exc:
            # Diagnostic only: numeric code + SDK status string are safe
            # scalars. Never log message/headers/bodies (may carry secrets).
            code = getattr(exc, "code", None)
            code = code if isinstance(code, int) else None
            sdk_status = getattr(exc, "status", None)
            sdk_status = (
                sdk_status if isinstance(sdk_status, str) else None
            )
            classification = _classify_upstream_code(code)
            logger.warning(
                "Gemini API request failed",
                extra={
                    "extra_fields": {
                        "error_type": type(exc).__name__,
                        "upstream_code": code,
                        "upstream_status": sdk_status,
                        "classification": classification,
                    }
                },
            )
            raise LLMProviderError("Language model request failed") from exc
        except Exception as exc:
            logger.warning(
                "Gemini request failed",
                extra={"extra_fields": {"error_type": type(exc).__name__}},
            )
            raise LLMProviderError("Language model request failed") from exc

        text = (getattr(response, "text", None) or "").strip()
        if not text:
            raise LLMInvalidResponseError(
                "Language model returned an invalid response"
            )
        return LLMResponse(content=text, model=self._model)
