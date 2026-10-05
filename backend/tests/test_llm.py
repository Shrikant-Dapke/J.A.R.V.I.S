"""Tests for the LLM provider abstraction (fake only, never the real API)."""

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.core.config import Settings
from app.main import app
from app.schemas.llm import LLMMessage, LLMResponse
from app.services.chat_service import get_chat_response
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


@pytest.fixture
def provider_override():
    """Pin FakeProvider (consistent with the conftest session guard)."""
    set_llm_provider_override(FakeProvider())
    clear_llm_provider_cache()
    yield
    set_llm_provider_override(FakeProvider())
    clear_llm_provider_cache()


@pytest_asyncio.fixture
async def client():
    """Create an async test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
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

    response = get_chat_response("Hello", conv_id)

    assert response.response == "recorded-reply"
    assert response.conversation_id == conv_id
    assert len(recorder.seen) == 1


def test_conversation_history_passed_to_provider(provider_override):
    """Provider receives full chronological history including new turn."""
    recorder = RecordingProvider()
    set_llm_provider_override(recorder)
    conv_id = str(uuid.uuid4())

    get_chat_response("First", conv_id)
    get_chat_response("Second", conv_id)

    assert len(recorder.seen) == 2
    first_call = [(m.role, m.content) for m in recorder.seen[0]]
    assert first_call == [("user", "First")]
    second_call = [(m.role, m.content) for m in recorder.seen[1]]
    assert second_call == [
        ("user", "First"),
        ("assistant", "recorded-reply"),
        ("user", "Second"),
    ]


def test_provider_failure_propagates_from_service(provider_override):
    """Service does not swallow provider errors."""
    set_llm_provider_override(FailingProvider(LLMProviderError("boom")))
    with pytest.raises(LLMProviderError):
        get_chat_response("Hello", str(uuid.uuid4()))


# Route error-mapping tests


@pytest.mark.asyncio
async def test_chat_makes_no_network_calls(
    client: AsyncClient, provider_override, monkeypatch
):
    """POST /api/chat succeeds with all socket creation blocked."""
    import socket

    def _blocked(*args, **kwargs):
        raise AssertionError("network call attempted during test")

    monkeypatch.setattr(socket, "socket", _blocked)
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
