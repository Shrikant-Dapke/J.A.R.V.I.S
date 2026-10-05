"""Session-wide test isolation: never touch the real LLM API.

This autouse fixture pins FakeProvider for every test, so a developer's
local .env (e.g. LLM_PROVIDER=gemini with a real key) can NEVER cause
ordinary tests to make network calls. Tests that need another provider
set an explicit override on top of this guard.
"""

import pytest

from app.services.llm.factory import (
    clear_llm_provider_cache,
    set_llm_provider_override,
)
from app.services.llm.fake import FakeProvider


@pytest.fixture(autouse=True)
def _force_fake_llm_provider():
    set_llm_provider_override(FakeProvider())
    clear_llm_provider_cache()
    yield
    set_llm_provider_override(FakeProvider())
    clear_llm_provider_cache()
