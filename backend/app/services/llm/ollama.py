"""Ollama local provider behind the LLM interface (text generation only).

Phase 1: Qwen 3.5 4B (qwen3.5:4b-q4_K_M) via the local Ollama runtime.
The model generates assistant text only: it never receives tool access
and cannot execute code. All model output remains untrusted text for
downstream policy ("the model proposes; policy authorizes").

This class speaks the Ollama REST API over HTTPX (no SDK) with
non-streaming ``POST /api/chat`` requests - a single bounded
request/response boundary that is easy to reuse and to test.

Thinking-mode handling (Qwen 3.5 is a thinking-capable model): the
request asks Ollama to keep thinking off (``"think": false``), and the
parser - regardless of server behavior - ignores any separate
``message.thinking`` field and strips the legacy inline thinking
marker blocks (angle-bracket THINK markers, as matched by
``_THINK_BLOCK_RE`` below) so that internal reasoning can never be
returned as the assistant answer. An answer that contains nothing after
stripping is a structured invalid-response error, never leaked
reasoning.

Failures are mapped to the shared LLM error classes with safe generic
messages; request/response bodies, prompts, and URLs are never logged.
"""

import re
from typing import Optional

import httpx

from app.core.config import (
    OLLAMA_MODEL_NAME_RE,
    OLLAMA_NUM_CTX_BOUNDS,
    OLLAMA_TIMEOUT_BOUNDS,
    validate_loopback_http_url,
)
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

CHAT_PATH = "/api/chat"

# Mirrors the defaults in app.core.config (single model, single runtime).
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.5:4b-q4_K_M"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_NUM_CTX = 4096

# Legacy thinking-mode representations. Newer Ollama versions place
# reasoning in a separate ``message.thinking`` field; older setups or
# forced templates may inline the reasoning inside the content between
# the classic thinking marker tags (an opening "towe" marker,
# reasoning text, and a closing "we" marker - paired or, on truncated
# generations, unclosed). Both shapes are internal reasoning and must
# never surface as the user-facing answer.
_THINK_BLOCK_RE = re.compile(
    r"<\s*think\s*>.*?<\s*/\s*think\s*>", re.DOTALL | re.IGNORECASE
)
_THINK_UNCLOSED_RE = re.compile(
    r"<\s*think\s*>.*\Z", re.DOTALL | re.IGNORECASE
)


def _classify_upstream_status(code: int) -> str:
    """Map an Ollama HTTP status to a short, safe classification."""
    if code == 400:
        return "bad-request"
    if code == 404:
        return "model-or-endpoint-not-found"
    if code == 429:
        return "overloaded"
    if 500 <= code < 600:
        return "upstream-server-error"
    return "unexpected-upstream-status"


def _strip_reasoning(content: str) -> str:
    """
    Remove internal reasoning from raw model output.

    Handles paired inline thinking blocks and unclosed (truncated)
    thinking markers - content without markers passes through
    unchanged. A separate ``thinking`` field never enters here: it is
    simply not read as content. Anything left of an unmatched opening
    marker is dropped entirely rather than risk leaking reasoning.
    """
    text = _THINK_BLOCK_RE.sub("", content)
    if _THINK_UNCLOSED_RE.search(text):
        text = _THINK_UNCLOSED_RE.sub("", text)
    return text.strip()


class OllamaProvider(LLMProvider):
    """LLMProvider backed by a local Ollama chat model.

    The HTTP client is created once and reused for every call; call
    :meth:`close` (or go via the provider factory) to release pooled
    connections. ``transport`` exists for tests so requests can be
    mocked without network access.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        num_ctx: int = DEFAULT_NUM_CTX,
        *,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        # Defense in depth: re-validate here so this class cannot be
        # pointed at an arbitrary remote host even if misused directly.
        try:
            self._base_url = validate_loopback_http_url(base_url).rstrip("/")
        except ValueError as exc:
            raise LLMConfigurationError(
                "Ollama base URL must be a loopback http(s) endpoint"
            ) from exc
        name = (model or "").strip()
        if not OLLAMA_MODEL_NAME_RE.match(name):
            raise LLMConfigurationError("Ollama model name is invalid")
        self._model = name
        timeout_low, timeout_high = OLLAMA_TIMEOUT_BOUNDS
        if not timeout_low <= float(timeout_seconds) <= timeout_high:
            raise LLMConfigurationError("Ollama timeout is out of range")
        self._timeout_seconds = float(timeout_seconds)
        ctx_low, ctx_high = OLLAMA_NUM_CTX_BOUNDS
        try:
            num_ctx_int = int(num_ctx)
        except (TypeError, ValueError) as exc:
            raise LLMConfigurationError("Ollama context size is invalid") from exc
        if not ctx_low <= num_ctx_int <= ctx_high:
            raise LLMConfigurationError("Ollama context size is out of range")
        self._num_ctx = num_ctx_int
        self._client = httpx.Client(
            base_url=self._base_url,
            # One bounded timeout for connect/read/write/pool.
            timeout=httpx.Timeout(self._timeout_seconds),
            transport=transport,
        )

    @property
    def model(self) -> str:
        """Configured Ollama model identifier."""
        return self._model

    def close(self) -> None:
        """Release pooled HTTP connections (client lifecycle hygiene)."""
        self._client.close()

    def _build_payload(self, messages: list[LLMMessage]) -> dict:
        """Build the non-streaming chat payload (roles map 1:1).

        ``think`` is explicitly disabled for Phase 1: the product wants
        the final assistant text, not a reasoning trace. Servers that
        predate the parameter simply ignore the unknown field, and the
        parser strips any reasoning shapes that still arrive.
        """
        return {
            "model": self._model,
            "messages": [
                {"role": m.role, "content": m.content} for m in messages
            ],
            "stream": False,
            "think": False,
            "options": {"num_ctx": self._num_ctx},
        }

    def _send_request(self, payload: dict) -> httpx.Response:
        try:
            response = self._client.post(CHAT_PATH, json=payload)
        except httpx.TimeoutException as exc:
            logger.warning(
                "Ollama request timed out",
                extra={"extra_fields": {"error_type": type(exc).__name__}},
            )
            raise LLMTimeoutError("Language model request timed out") from exc
        except httpx.TransportError as exc:
            # Covers connection refused while Ollama is down or the
            # model/port is otherwise unreachable.
            logger.warning(
                "Ollama service unreachable",
                extra={"extra_fields": {"error_type": type(exc).__name__}},
            )
            raise LLMProviderError(
                "Language model service is unavailable"
            ) from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Diagnostic only: numeric status + classification. Bodies,
            # prompts, and URLs are never logged (no secrets/content).
            status = exc.response.status_code
            logger.warning(
                "Ollama request failed",
                extra={
                    "extra_fields": {
                        "upstream_status": status,
                        "classification": _classify_upstream_status(status),
                        "error_type": type(exc).__name__,
                    }
                },
            )
            raise LLMProviderError("Language model request failed") from exc
        return response

    def _parse_response(self, response: httpx.Response) -> LLMResponse:
        try:
            data = response.json()
        except ValueError as exc:
            # Malformed JSON / undecodable body.
            logger.warning(
                "Ollama response was not parseable",
                extra={"extra_fields": {"error_type": type(exc).__name__}},
            )
            raise LLMInvalidResponseError(
                "Language model returned an invalid response"
            ) from exc

        if not isinstance(data, dict) or "error" in data:
            raise LLMInvalidResponseError(
                "Language model returned an invalid response"
            )
        message = data.get("message")
        if not isinstance(message, dict):
            raise LLMInvalidResponseError(
                "Language model returned an invalid response"
            )
        # NOTE: only ``content`` is read. Any ``thinking``/reasoning
        # fields on the message are deliberately ignored so internal
        # reasoning cannot reach the caller.
        content = message.get("content")
        if not isinstance(content, str):
            raise LLMInvalidResponseError(
                "Language model returned an invalid response"
            )
        text = _strip_reasoning(content)
        if not text:
            raise LLMInvalidResponseError(
                "Language model returned an empty response"
            )
        raw_model = data.get("model")
        resolved_model = (
            raw_model.strip()
            if isinstance(raw_model, str) and raw_model.strip()
            else self._model
        )
        return LLMResponse(content=text, model=resolved_model)

    def generate_response(self, messages: list[LLMMessage]) -> LLMResponse:
        """Generate an assistant reply for the given conversation history.

        Raises:
            LLMConfigurationError: On invalid provider configuration.
            LLMTimeoutError: When the bounded request timeout is hit.
            LLMProviderError: On connection/HTTP failures.
            LLMInvalidResponseError: On empty, malformed, or
                structurally invalid responses.
        """
        response = self._send_request(self._build_payload(messages))
        return self._parse_response(response)
