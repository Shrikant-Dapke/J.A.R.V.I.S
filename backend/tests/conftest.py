"""Session-wide test isolation: never touch the real LLM API.

This autouse fixture pins FakeProvider for every test, so a developer's
local .env (e.g. LLM_PROVIDER=gemini with a real key) can NEVER cause
ordinary tests to make network calls. Tests that need another provider
set an explicit override on top of this guard.
"""

import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.services.llm.factory import (
    clear_llm_provider_cache,
    set_llm_provider_override,
)
from app.services.llm.fake import FakeProvider


TEST_API_TOKEN = "test-api-token-012345678901234567890123456789"
TEST_SESSION_ID = "test-session-012345678901234567"
OTHER_SESSION_ID = "other-session-012345678901234567"


@pytest.fixture
def api_headers():
    """Headers used by authenticated local API client tests."""
    return {
        "Authorization": f"Bearer {TEST_API_TOKEN}",
        "X-JARVIS-Session": TEST_SESSION_ID,
    }


@pytest.fixture
def other_session_headers():
    """Headers of a second authenticated session of the same owner."""
    return {
        "Authorization": f"Bearer {TEST_API_TOKEN}",
        "X-JARVIS-Session": OTHER_SESSION_ID,
    }


@pytest.fixture(autouse=True)
def _configure_local_api_auth():
    """Provide only the test client with a deterministic API secret."""
    previous = settings.api_token
    settings.api_token = SecretStr(TEST_API_TOKEN)
    yield
    settings.api_token = previous


@pytest.fixture(autouse=True)
def _force_fake_llm_provider():
    set_llm_provider_override(FakeProvider())
    clear_llm_provider_cache()
    yield
    set_llm_provider_override(FakeProvider())
    clear_llm_provider_cache()
