"""Session-wide test isolation: never touch the real LLM API.

This autouse fixture pins FakeProvider for every test, so a developer's
local .env (e.g. LLM_PROVIDER=gemini with a real key) can NEVER cause
ordinary tests to make network calls. Tests that need another provider
set an explicit override on top of this guard.

Phase 4.3 execution-state isolation: the shared file-backed execution
database is redirected into a fresh temporary directory for this pytest
process, so service-level singletons never touch the developer's data
directory and every test run starts from clean persisted state.
"""

import atexit
import os
import shutil
import tempfile

# The path must be set BEFORE any app module is imported: pydantic
# settings read the environment when app.core.config is first imported.
_TEST_STATE_DIR = tempfile.mkdtemp(prefix="jarvis-state-test-")
os.environ["JARVIS_STATE_DB_PATH"] = os.path.join(
    _TEST_STATE_DIR, "jarvis_state.db"
)

atexit.register(
    lambda: shutil.rmtree(_TEST_STATE_DIR, ignore_errors=True)
)

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
