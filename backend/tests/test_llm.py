"""Tests for the LLM provider abstraction (mocked, never the real APIs).

All provider HTTP calls are mocked (Gemini fake client object,
Ollama httpx.MockTransport); tests never require Ollama or Gemini to
be installed or reachable.
"""

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.core.config import Settings, settings
from app.main import app
from app.schemas.llm import LLMMessage, LLMResponse
from app.services.chat_service import get_chat_response
from app.services.orchestrator import ORCHESTRATOR_INSTRUCTION_PREFIX
from app.services.conversation_store import (
    ConversationNotFoundError,
    conversation_store,
)
from app.services.llm.base import LLMProvider
from app.services.llm.errors import (
    LLMConfigurationError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMTimeoutError,
)
from app.services.llm.factory import (
    clear_llm_provider_cache,
    get_llm_provider,
    set_llm_provider_override,
)
from app.services.llm.fake import PLACEHOLDER_RESPONSE, FakeProvider
from app.services.llm.gemini import GeminiProvider
from app.services.llm.ollama import OllamaProvider

SERVICE_OWNER_ID = "hash-of-test-owner"
SERVICE_SESSION_ID = "llm-test-session-0123456789"


@pytest.fixture
def provider_override():
    """Pin FakeProvider (consistent with the conftest session guard)."""
    set_llm_provider_override(FakeProvider())
    clear_llm_provider_cache()
    yield
    set_llm_provider_override(FakeProvider())
    clear_llm_provider_cache()


@pytest_asyncio.fixture
async def client(api_headers):
    """Create an async test client for the authenticated local owner."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        headers=api_headers,
    ) as ac:
        yield ac


class RecordingProvider(LLMProvider):
    """Fake that records received messages and replies deterministically."""

    def __init__(self) -> None:
        self.seen: list[list[LLMMessage]] = []

    def generate_response(self, messages: list[LLMMessage]) -> LLMResponse:
        self.seen.append(list(messages))
        return LLMResponse(content="recorded-reply", model="recording")


class FailingProvider(LLMProvider):
    """Fake that always raises the given error."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def generate_response(self, messages: list[LLMMessage]) -> LLMResponse:
        raise self._error


# Interface tests


def test_fake_provider_implements_interface(provider_override):
    """FakeProvider satisfies the LLMProvider contract."""
    assert isinstance(FakeProvider(), LLMProvider)
    result = FakeProvider().generate_response(
        [LLMMessage(role="user", content="Hi")]
    )
    assert result.content == PLACEHOLDER_RESPONSE
    assert result.model == "fake"


def test_default_provider_is_fake(provider_override):
    """Without configuration the factory returns the offline fake."""
    assert isinstance(get_llm_provider(), FakeProvider)


# Service tests


def test_chat_service_uses_provider(provider_override):
    """Chat service returns provider text and stores both turns."""
    recorder = RecordingProvider()
    set_llm_provider_override(recorder)
    conv_id = str(uuid.uuid4())

    response = get_chat_response(
        "Hello",
        conv_id,
        owner_id=SERVICE_OWNER_ID,
        session_id=SERVICE_SESSION_ID,
    )

    assert response.response == "recorded-reply"
    assert response.conversation_id == conv_id
    assert len(recorder.seen) == 1


def test_conversation_history_passed_to_provider(provider_override):
    """Provider receives full chronological history plus the orchestrator
    decision instruction.

    Phase 4 contract change: the Main Orchestrator appends one trusted,
    bounded instruction message as the FINAL user turn of the same
    provider call. The instruction is never persisted in the
    conversation history; the caller's turns are forwarded untouched.
    """
    recorder = RecordingProvider()
    set_llm_provider_override(recorder)
    conv_id = str(uuid.uuid4())

    get_chat_response(
        "First", conv_id, owner_id=SERVICE_OWNER_ID, session_id=SERVICE_SESSION_ID
    )
    get_chat_response(
        "Second", conv_id, owner_id=SERVICE_OWNER_ID, session_id=SERVICE_SESSION_ID
    )

    # One bounded decision call per turn (reply text never triggers a summary call).
    assert len(recorder.seen) == 2

    first_call = [(m.role, m.content) for m in recorder.seen[0]]
    # History precedes the appended instruction message.
    assert first_call[:-1] == [("user", "First")]
    first_instruction_role, first_instruction_content = first_call[-1]
    assert first_instruction_role == "user"
    assert ORCHESTRATOR_INSTRUCTION_PREFIX in first_instruction_content
    assert "USER REQUEST:" in first_instruction_content
    assert "First" in first_instruction_content

    second_call = [(m.role, m.content) for m in recorder.seen[1]]
    assert second_call[:-1] == [
        ("user", "First"),
        ("assistant", "recorded-reply"),
        ("user", "Second"),
    ]
    second_instruction_role, second_instruction_content = second_call[-1]
    assert second_instruction_role == "user"
    assert ORCHESTRATOR_INSTRUCTION_PREFIX in second_instruction_content
    assert "USER REQUEST:" in second_instruction_content
    assert "Second" in second_instruction_content


def test_chat_service_never_appends_to_foreign_conversation(provider_override):
    """A conversation of another identity raises instead of being reused."""
    recorder = RecordingProvider()
    set_llm_provider_override(recorder)
    conv_id = str(uuid.uuid4())

    get_chat_response(
        "First", conv_id, owner_id=SERVICE_OWNER_ID, session_id=SERVICE_SESSION_ID
    )
    with pytest.raises(ConversationNotFoundError):
        get_chat_response(
            "Hijack",
            conv_id,
            owner_id="hash-of-other-owner",
            session_id="llm-other-session-012345678",
        )

    # The second call must not have reached the provider or stored turns.
    assert len(recorder.seen) == 1
    history = conversation_store.get_history(
        conv_id, owner_id=SERVICE_OWNER_ID, session_id=SERVICE_SESSION_ID
    )
    assert [t.content for t in history] == ["First", "recorded-reply"]
    assert (
        conversation_store.get_history(
            conv_id,
            owner_id="hash-of-other-owner",
            session_id="llm-other-session-012345678",
        )
        is None
    )


def test_provider_failure_propagates_from_service(provider_override):
    """Service does not swallow provider errors."""
    set_llm_provider_override(FailingProvider(LLMProviderError("boom")))
    with pytest.raises(LLMProviderError):
        get_chat_response(
            "Hello",
            str(uuid.uuid4()),
            owner_id=SERVICE_OWNER_ID,
            session_id=SERVICE_SESSION_ID,
        )


# Route error-mapping tests


@pytest.mark.asyncio
async def test_chat_makes_no_network_calls(
    client: AsyncClient, provider_override, monkeypatch
):
    """POST /api/chat succeeds with all socket creation blocked."""
    import socket

    class _BlockedSocket(socket.socket):
        """Any socket instantiation must fail the test immediately."""

        def __init__(self, *args, **kwargs):
            raise AssertionError("network call attempted during test")

    # Replace the class (not with a callable) so alive event-loop internals
    # that check isinstance(..., socket.socket) stay functional on Windows.
    monkeypatch.setattr(socket, "socket", _BlockedSocket)
    response = await client.post("/api/chat", json={"message": "Hello"})
    assert response.status_code == 200
    assert response.json()["response"] == PLACEHOLDER_RESPONSE


@pytest.mark.asyncio
async def test_api_response_compatible(client: AsyncClient, provider_override):
    """API contract unchanged with provider-backed service."""
    response = await client.post("/api/chat", json={"message": "Hello"})
    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) >= {
        "conversation_id",
        "response",
        "status",
        "timestamp",
    }


@pytest.mark.asyncio
async def test_provider_failure_structured_502(
    client: AsyncClient, provider_override
):
    """Provider failure maps to structured 502 without internals."""
    set_llm_provider_override(FailingProvider(LLMProviderError("boom")))
    response = await client.post("/api/chat", json={"message": "Hello"})
    assert response.status_code == 502
    data = response.json()
    assert data["error"]["code"] == "HTTP_502"
    assert "request_id" in data["error"]
    assert "boom" not in data["error"]["message"]


@pytest.mark.asyncio
async def test_timeout_structured_504(client: AsyncClient, provider_override):
    """Timeout maps to structured 504."""
    set_llm_provider_override(FailingProvider(LLMTimeoutError("slow")))
    response = await client.post("/api/chat", json={"message": "Hello"})
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "HTTP_504"


@pytest.mark.asyncio
async def test_configuration_failure_structured_500(
    client: AsyncClient, provider_override
):
    """Configuration failure maps to structured 500."""
    set_llm_provider_override(FailingProvider(LLMConfigurationError("no key")))
    response = await client.post("/api/chat", json={"message": "Hello"})
    assert response.status_code == 500
    data = response.json()
    assert data["error"]["code"] == "HTTP_500"
    assert "no key" not in data["error"]["message"]


@pytest.mark.asyncio
async def test_invalid_response_structured_502(
    client: AsyncClient, provider_override
):
    """Invalid provider response maps to structured 502."""
    set_llm_provider_override(
        FailingProvider(LLMInvalidResponseError("empty"))
    )
    response = await client.post("/api/chat", json={"message": "Hello"})
    assert response.status_code == 502


# Gemini provider unit tests (mocked client, no network)


def test_gemini_requires_api_key():
    """Missing key fails fast with a configuration error."""
    with pytest.raises(LLMConfigurationError):
        GeminiProvider(api_key=None)
    with pytest.raises(LLMConfigurationError):
        GeminiProvider(api_key="   ")


def test_gemini_success_maps_assistant_role():
    """Gemini provider returns text and maps assistant to model role."""
    provider = GeminiProvider(api_key="test-key", model="test-model")
    captured = {}

    def fake_generate_content(model, contents):
        captured["model"] = model
        captured["contents"] = contents
        return SimpleNamespace(text="  mocked reply  ")

    provider._client = SimpleNamespace(
        models=SimpleNamespace(generate_content=fake_generate_content)
    )

    result = provider.generate_response(
        [
            LLMMessage(role="user", content="Hi"),
            LLMMessage(role="assistant", content="Hello"),
        ]
    )
    assert result.content == "mocked reply"
    assert result.model == "test-model"
    assert captured["model"] == "test-model"
    assert [c["role"] for c in captured["contents"]] == ["user", "model"]


def test_gemini_empty_text_invalid():
    """Empty model text raises an invalid-response error."""
    provider = GeminiProvider(api_key="test-key")
    provider._client = SimpleNamespace(
        models=SimpleNamespace(
            generate_content=lambda model, contents: SimpleNamespace(text="  ")
        )
    )
    with pytest.raises(LLMInvalidResponseError):
        provider.generate_response([LLMMessage(role="user", content="Hi")])


def test_gemini_sdk_failure_maps_to_provider_error():
    """SDK exceptions map to provider errors without leaking details."""
    provider = GeminiProvider(api_key="test-key")

    def boom(model, contents):
        raise RuntimeError("sdk exploded")

    provider._client = SimpleNamespace(
        models=SimpleNamespace(generate_content=boom)
    )
    with pytest.raises(LLMProviderError):
        provider.generate_response([LLMMessage(role="user", content="Hi")])


def test_gemini_timeout_maps_to_timeout_error():
    """Timeouts map to timeout errors."""
    provider = GeminiProvider(api_key="test-key")

    def slow(model, contents):
        raise TimeoutError("timed out")

    provider._client = SimpleNamespace(
        models=SimpleNamespace(generate_content=slow)
    )
    with pytest.raises(LLMTimeoutError):
        provider.generate_response([LLMMessage(role="user", content="Hi")])


# Configuration validation tests


def test_llm_provider_valid_values():
    """Valid provider names are accepted (case-insensitive)."""
    assert Settings(llm_provider="fake").llm_provider == "fake"
    assert Settings(llm_provider="GEMINI").llm_provider == "gemini"


def test_llm_provider_invalid_rejected():
    """Unknown provider names are rejected."""
    with pytest.raises(ValueError) as exc_info:
        Settings(llm_provider="openai")
    assert "llm_provider must be one of" in str(exc_info.value)


# Ollama provider unit tests (mocked transport, no network)


OLLAMA_REPLY = {
    "model": "qwen3.5:4b-q4_K_M",
    "created_at": "2026-01-01T00:00:00.000000000Z",
    "message": {"role": "assistant", "content": "local reply"},
    "done": True,
}


def _ollama_provider(handler, **overrides):
    """Build an OllamaProvider whose HTTP layer is a MockTransport."""
    return OllamaProvider(
        base_url="http://127.0.0.1:11434",
        model="qwen3.5:4b-q4_K_M",
        timeout_seconds=5.0,
        num_ctx=2048,
        transport=httpx.MockTransport(handler),
        **overrides,
    )


def test_ollama_provider_implements_interface():
    """OllamaProvider satisfies the LLMProvider contract."""
    provider = _ollama_provider(
        lambda request: httpx.Response(200, json=OLLAMA_REPLY, request=request)
    )
    assert isinstance(provider, LLMProvider)
    result = provider.generate_response(
        [LLMMessage(role="user", content="Hi")]
    )
    assert result.content == "local reply"


def test_ollama_success_returns_content_and_model_metadata():
    """Content is parsed from message.content, model from response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=OLLAMA_REPLY, request=request)

    result = _ollama_provider(handler).generate_response(
        [LLMMessage(role="user", content="Hi")]
    )
    assert result.content == "local reply"
    assert result.model == "qwen3.5:4b-q4_K_M"


# Thinking-mode tests (Qwen 3.5 is thinking-capable; internal reasoning
# must never reach the caller in any of Ollama's response shapes).


_O = "<" + "think" + ">"  # thinking marker, assembled to avoid confusion
_C = "<" + "/" + "think" + ">"


def test_ollama_separate_thinking_field_is_not_returned():
    """A message.thinking field is ignored; only content is returned."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(OLLAMA_REPLY)
        body["message"] = {
            "role": "assistant",
            "content": "final answer",
            "thinking": "internal reasoning seed-internal-token",
        }
        return httpx.Response(200, json=body, request=request)

    result = _ollama_provider(handler).generate_response(
        [LLMMessage(role="user", content="Hi")]
    )
    assert result.content == "final answer"
    assert "internal reasoning" not in result.content
    assert "seed-internal-token" not in result.content


def test_ollama_thinking_only_response_is_invalid():
    """Empty content with only internal reasoning is a safe error."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(OLLAMA_REPLY)
        body["message"] = {
            "role": "assistant",
            "content": "",
            "thinking": "all reasoning, no answer",
        }
        return httpx.Response(200, json=body, request=request)

    with pytest.raises(LLMInvalidResponseError):
        _ollama_provider(handler).generate_response(
            [LLMMessage(role="user", content="Hi")]
        )


def test_ollama_think_markers_are_stripped():
    """Legacy inline thinking blocks never surface as the answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(OLLAMA_REPLY)
        body["message"] = {
            "role": "assistant",
            "content": _O
            + "hidden reasoning trace"
            + _C
            + "the visible answer",
        }
        return httpx.Response(200, json=body, request=request)

    result = _ollama_provider(handler).generate_response(
        [LLMMessage(role="user", content="Hi")]
    )
    assert result.content == "the visible answer"
    assert "hidden reasoning" not in result.content


def test_ollama_whitespace_only_after_marker_stripping_is_invalid():
    """Content that is hollow after marker removal raises invalid."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(OLLAMA_REPLY)
        body["message"] = {
            "role": "assistant",
            "content": "   " + _O + "reasoning" + _C + "   ",
        }
        return httpx.Response(200, json=body, request=request)

    with pytest.raises(LLMInvalidResponseError):
        _ollama_provider(handler).generate_response(
            [LLMMessage(role="user", content="Hi")]
        )


def test_ollama_unclosed_think_marker_drops_reasoning_tail():
    """Truncated generation: unclosed marker strips to end of content."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(OLLAMA_REPLY)
        body["message"] = {
            "role": "assistant",
            "content": _O + "leaky truncated reasoning",
        }
        return httpx.Response(200, json=body, request=request)

    with pytest.raises(LLMInvalidResponseError):
        _ollama_provider(handler).generate_response(
            [LLMMessage(role="user", content="Hi")]
        )


def test_ollama_plain_content_without_thinking_is_untouched():
    """Responses with no reasoning shapes pass through exactly."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(OLLAMA_REPLY)
        body["message"] = {"role": "assistant", "content": "plain text"}
        return httpx.Response(200, json=body, request=request)

    assert (
        _ollama_provider(handler).generate_response(
            [LLMMessage(role="user", content="Hi")]
        ).content
        == "plain text"
    )


def test_ollama_falls_back_to_configured_model_metadata():
    """A missing upstream model field falls back to the configured name."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(OLLAMA_REPLY)
        body.pop("model")
        return httpx.Response(200, json=body, request=request)

    result = _ollama_provider(handler).generate_response(
        [LLMMessage(role="user", content="Hi")]
    )
    assert result.model == "qwen3.5:4b-q4_K_M"


def test_ollama_sends_expected_payload_and_maps_history():
    """POST /api/chat with stream off, num_ctx, and 1:1 role mapping."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json=OLLAMA_REPLY, request=request)

    provider = _ollama_provider(handler)
    result = provider.generate_response(
        [
            LLMMessage(role="user", content="Hi"),
            LLMMessage(role="assistant", content="Hello"),
            LLMMessage(role="user", content="What can you do?"),
        ]
    )
    assert result.content == "local reply"
    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    assert captured["payload"] == {
        "model": "qwen3.5:4b-q4_K_M",
        "messages": [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "What can you do?"},
        ],
        "stream": False,
        "think": False,
        "options": {"num_ctx": 2048},
    }


def test_ollama_empty_content_is_invalid():
    """Whitespace-only model output raises an invalid-response error."""
    for content in ("", "   "):

        def handler(request: httpx.Request, _c=content) -> httpx.Response:
            body = dict(OLLAMA_REPLY)
            body["message"] = {"role": "assistant", "content": _c}
            return httpx.Response(200, json=body, request=request)

        with pytest.raises(LLMInvalidResponseError):
            _ollama_provider(handler).generate_response(
                [LLMMessage(role="user", content="Hi")]
            )


def test_ollama_missing_message_is_invalid():
    """A body without a message object raises invalid-response."""
    body = {k: v for k, v in OLLAMA_REPLY.items() if k != "message"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body, request=request)

    with pytest.raises(LLMInvalidResponseError):
        _ollama_provider(handler).generate_response(
            [LLMMessage(role="user", content="Hi")]
        )


def test_ollama_error_body_is_invalid():
    """An error body (HTTP 200) is not turned into assistant text."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"error": "unknown model"}, request=request
        )

    with pytest.raises(LLMInvalidResponseError):
        _ollama_provider(handler).generate_response(
            [LLMMessage(role="user", content="Hi")]
        )


def test_ollama_non_string_content_is_invalid():
    """Non-string message content raises invalid-response."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(OLLAMA_REPLY)
        body["message"] = {"role": "assistant", "content": 12345}
        return httpx.Response(200, json=body, request=request)

    with pytest.raises(LLMInvalidResponseError):
        _ollama_provider(handler).generate_response(
            [LLMMessage(role="user", content="Hi")]
        )


def test_ollama_non_dict_body_is_invalid():
    """A JSON array body raises invalid-response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[OLLAMA_REPLY], request=request)

    with pytest.raises(LLMInvalidResponseError):
        _ollama_provider(handler).generate_response(
            [LLMMessage(role="user", content="Hi")]
        )


def test_ollama_malformed_json_is_invalid():
    """Truncated/invalid JSON raises invalid-response."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b'{"message": {"content": "hi"', request=request
        )

    with pytest.raises(LLMInvalidResponseError):
        _ollama_provider(handler).generate_response(
            [LLMMessage(role="user", content="Hi")]
        )


def test_ollama_connection_refused_maps_to_provider_error():
    """Unreachable service maps to a provider error."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LLMProviderError):
        _ollama_provider(handler).generate_response(
            [LLMMessage(role="user", content="Hi")]
        )


def test_ollama_timeout_maps_to_timeout_error():
    """Timeouts map to timeout errors."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    with pytest.raises(LLMTimeoutError):
        _ollama_provider(handler).generate_response(
            [LLMMessage(role="user", content="Hi")]
        )


@pytest.mark.parametrize("status_code", [400, 404, 500, 503])
def test_ollama_http_error_maps_to_provider_error(status_code):
    """HTTP error statuses map to provider errors without leaking."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"error": "leaky-internal-trace-value"},
            request=request,
        )

    with pytest.raises(LLMProviderError) as exc_info:
        _ollama_provider(handler).generate_response(
            [LLMMessage(role="user", content="Hi")]
        )
    assert "leaky-internal-trace-value" not in str(exc_info.value)


def test_ollama_provider_rejects_non_loopback_base_url():
    """The provider refuses URLs that could leave the machine."""
    with pytest.raises(LLMConfigurationError):
        OllamaProvider(base_url="http://203.0.113.5:11434")
    with pytest.raises(LLMConfigurationError):
        OllamaProvider(base_url="ftp://127.0.0.1:11434")


def test_ollama_provider_rejects_invalid_model_and_bounds():
    """Model name, timeout, and context constructor validation."""
    with pytest.raises(LLMConfigurationError):
        OllamaProvider(model="bad model name!")
    with pytest.raises(LLMConfigurationError):
        OllamaProvider(timeout_seconds=0.5)
    with pytest.raises(LLMConfigurationError):
        OllamaProvider(timeout_seconds=601.0)
    with pytest.raises(LLMConfigurationError):
        OllamaProvider(num_ctx=100)
    with pytest.raises(LLMConfigurationError):
        OllamaProvider(num_ctx=100_000)


# Factory selection with Ollama configured (no network: client is not used)


def test_factory_selects_ollama(monkeypatch):
    """llm_provider=ollama selects and caches the Ollama provider."""
    clear_llm_provider_cache()
    set_llm_provider_override(None)
    monkeypatch.setattr(settings, "llm_provider", "ollama")
    try:
        provider = get_llm_provider()
        assert isinstance(provider, OllamaProvider)
        assert provider.model == "qwen3.5:4b-q4_K_M"
        assert get_llm_provider() is provider  # cached client reused
    finally:
        set_llm_provider_override(FakeProvider())
        clear_llm_provider_cache()


def test_provider_override_beats_ollama_configuration(monkeypatch):
    """Explicit overrides still win over settings configuration."""
    override = FakeProvider()
    set_llm_provider_override(override)
    monkeypatch.setattr(settings, "llm_provider", "ollama")
    try:
        assert get_llm_provider() is override
    finally:
        set_llm_provider_override(FakeProvider())
        clear_llm_provider_cache()


@pytest.mark.asyncio
async def test_chat_route_with_mocked_ollama_provider(
    client: AsyncClient, provider_override
):
    """End-to-end chat with a mocked Ollama provider (no network).

    The mocked response carries internal reasoning in both shapes
    (separate thinking field plus inline markers); none may reach the
    user-facing response.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(OLLAMA_REPLY)
        body["message"] = {
            "role": "assistant",
            "content": _O + "hidden router reasoning" + _C + "route reply",
            "thinking": "separate-field router reasoning seed-route",
        }
        return httpx.Response(200, json=body, request=request)

    set_llm_provider_override(_ollama_provider(handler))
    response = await client.post("/api/chat", json={"message": "Hello"})
    assert response.status_code == 200
    assert response.json()["response"] == "route reply"
    assert "hidden router reasoning" not in response.text
    assert "seed-route" not in response.text


# Ollama settings validation


def test_ollama_settings_defaults_target_phase1_model():
    """Phase 1 Ollama defaults: qwen3.5:4b-q4_K_M, 120s bounded timeout,
    4096-token context on the loopback runtime.

    llm_provider's default is asserted by the offline-default factory
    test (Settings reflects the local .env otherwise).
    """
    assert Settings().ollama_model == "qwen3.5:4b-q4_K_M"
    assert Settings().ollama_timeout_seconds == 120.0
    assert Settings().ollama_num_ctx == 4096
    assert Settings().ollama_base_url == "http://127.0.0.1:11434"


def test_llm_provider_accepts_ollama():
    """ollama is a permitted provider name (case-insensitive)."""
    assert Settings(llm_provider="OLLAMA").llm_provider == "ollama"


def test_ollama_settings_accept_loopback_urls():
    """Loopback literals and localhost are accepted."""
    assert (
        Settings(ollama_base_url="http://127.0.0.1:11434").ollama_base_url
        == "http://127.0.0.1:11434"
    )
    assert (
        Settings(ollama_base_url="http://localhost:11434").ollama_base_url
        == "http://localhost:11434"
    )
    assert (
        Settings(ollama_base_url="http://[::1]:11434").ollama_base_url
        == "http://[::1]:11434"
    )


def test_ollama_settings_reject_remote_hosts():
    """Non-loopback endpoints are rejected at configuration time."""
    with pytest.raises(ValueError) as loopback_info:
        Settings(ollama_base_url="http://198.51.100.5:11434")
    assert "ollama_base_url" in str(loopback_info.value)
    with pytest.raises(ValueError):
        Settings(ollama_base_url="http://example.com:11434")
    with pytest.raises(ValueError):
        Settings(ollama_base_url="ftp://127.0.0.1:11434")
    with pytest.raises(ValueError):
        Settings(ollama_base_url="http://user:pass@127.0.0.1:11434")
    with pytest.raises(ValueError):
        Settings(ollama_base_url="http://127.0.0.1:11434/api")


def test_ollama_settings_reject_bad_model_and_bounds():
    """Model identifier and numeric bounds are validated."""
    with pytest.raises(ValueError):
        Settings(ollama_model="")
    with pytest.raises(ValueError):
        Settings(ollama_model="bad model name!")
    with pytest.raises(ValueError):
        Settings(ollama_timeout_seconds=0.1)
    with pytest.raises(ValueError):
        Settings(ollama_num_ctx=100)
    assert Settings(ollama_num_ctx=2048).ollama_num_ctx == 2048
