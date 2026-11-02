"""Execution reliability over the HTTP API (Phase 4.3).

Covers the new resolve-recovery endpoint and API-level execution
isolation. Plans are created through the real /api/chat flow with a
scripted provider, exactly like the phase-4 orchestration API tests.

The shared approval executor is patched per test so the mutating step
times out deterministically WITHOUT launching any real application.
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.tool import ToolResult
from app.services.llm.base import LLMProvider, LLMResponse
from app.services.llm.errors import LLMProviderError
from app.services.llm.factory import (
    clear_llm_provider_cache,
    set_llm_provider_override,
)
from app.services.tool_executor import default_tool_executor


DECISION_PLAN = (
    '{"action": "plan", "goal": "Then notepad", '
    '"explanation": "One mutating step.", "steps": ['
    '{"step_id": "s1", "description": "Open notepad", '
    '"tool_name": "open_application", "arguments": {"app_id": "notepad"}, '
    '"depends_on": []}]}'
)
DECISION_READONLY_PLAN = (
    '{"action": "plan", "goal": "Read info", '
    '"explanation": "One read-only step.", "steps": ['
    '{"step_id": "s1", "description": "Read system info", '
    '"tool_name": "get_system_info", "arguments": {}, '
    '"depends_on": []}]}'
)


class ScriptedProvider(LLMProvider):
    """Queued responses; raising the same assertion when exhausted."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[list] = []

    def generate_response(self, messages):
        self.calls.append(list(messages))
        if len(self.calls) > len(self._responses):
            raise LLMProviderError("unexpected extra provider call")
        item = self._responses[len(self.calls) - 1]
        if isinstance(item, Exception):
            raise item
        return LLMResponse(content=item, model="scripted")


def _use_provider(provider):
    set_llm_provider_override(provider)
    clear_llm_provider_cache()


@pytest.fixture(autouse=True)
def _restore_fake_provider():
    yield
    set_llm_provider_override(None)
    clear_llm_provider_cache()


@pytest_asyncio.fixture
async def client(api_headers):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://127.0.0.1", headers=api_headers
    ) as ac:
        yield ac


def _patch_timeout():
    """A deterministic mutating timeout at the execution boundary."""
    def timeout_execute(name, arguments=None, **kwargs):
        return ToolResult.failure_result(
            tool_name=name,
            error_code="execution_timeout",
            message="The tool did not finish within its execution budget.",
        )

    return timeout_execute


@pytest.mark.asyncio
async def test_resolve_recovery_404_for_unknown_plan(client: AsyncClient):
    response = await client.post(
        f"/api/plans/{uuid4()}/steps/s1/resolve-recovery"
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_resolve_recovery_404_for_unknown_step(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(
        default_tool_executor, "execute", _patch_timeout()
    )
    _use_provider(ScriptedProvider([DECISION_PLAN]))
    created = await client.post("/api/chat", json={"message": "Plan it"})
    clear_llm_provider_cache()
    plan_id = created.json()["plan"]["plan_id"]

    response = await client.post(
        f"/api/plans/{plan_id}/steps/unknown-step/resolve-recovery"
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_resolve_recovery_foreign_session_observes_404(
    client: AsyncClient, other_session_headers, monkeypatch
):
    monkeypatch.setattr(
        default_tool_executor, "execute", _patch_timeout()
    )
    _use_provider(ScriptedProvider([DECISION_PLAN]))
    created = await client.post("/api/chat", json={"message": "Plan it"})
    clear_llm_provider_cache()
    plan_id = created.json()["plan"]["plan_id"]

    approval = await client.post(f"/api/plans/{plan_id}/steps/s1/request")
    assert approval.status_code == 202
    approval_id = approval.json()["approval_id"]

    approved = await client.post(f"/api/approvals/{approval_id}/approve")
    assert approved.status_code == 200

    foreign = await client.post(
        f"/api/plans/{plan_id}/steps/s1/resolve-recovery",
        headers={
            "Authorization": other_session_headers["Authorization"],
            "X-JARVIS-Session": other_session_headers["X-JARVIS-Session"],
        },
    )
    assert foreign.status_code == 404
    # The uncertain step remains intact for its owner.
    plan = await client.get(f"/api/plans/{plan_id}")
    assert plan.json()["steps"][0]["status"] == "RECOVERY_REQUIRED"


@pytest.mark.asyncio
async def test_resolve_recovery_409_when_not_required(client: AsyncClient):
    _use_provider(ScriptedProvider([DECISION_READONLY_PLAN]))
    created = await client.post("/api/chat", json={"message": "Plan it"})
    clear_llm_provider_cache()
    plan_id = created.json()["plan"]["plan_id"]

    executed = await client.post(f"/api/plans/{plan_id}/steps/s1/request")
    assert executed.status_code == 200
    assert executed.json()["success"] is True

    conflict = await client.post(
        f"/api/plans/{plan_id}/steps/s1/resolve-recovery"
    )
    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_mutating_timeout_then_resolve_recovery_lifecycle(
    client: AsyncClient, monkeypatch
):
    monkeypatch.setattr(
        default_tool_executor, "execute", _patch_timeout()
    )
    _use_provider(ScriptedProvider([DECISION_PLAN]))
    created = await client.post("/api/chat", json={"message": "Plan it"})
    clear_llm_provider_cache()
    plan_id = created.json()["plan"]["plan_id"]

    # 1. Execution request: pending approval, 202, nothing executed.
    requested = await client.post(f"/api/plans/{plan_id}/steps/s1/request")
    assert requested.status_code == 202
    approval_id = requested.json()["approval_id"]
    assert requested.json()["tool_name"] == "open_application"

    # 2. A duplicate request for the ongoing step conflicts (409).
    duplicate = await client.post(f"/api/plans/{plan_id}/steps/s1/request")
    assert duplicate.status_code == 409

    # 3. Approval consumes single-use and reports the timed-out outcome.
    approved = await client.post(f"/api/approvals/{approval_id}/approve")
    assert approved.status_code == 200
    assert approved.json()["success"] is False
    assert approved.json()["error_code"] == "execution_timeout"

    # 4. The step is honest about uncertainty.
    plan = await client.get(f"/api/plans/{plan_id}")
    assert plan.status_code == 200
    assert plan.json()["steps"][0]["status"] == "RECOVERY_REQUIRED"

    # 5. Explicit reconciliation resolves it FAILED(uncertain), exactly once.
    resolved = await client.post(
        f"/api/plans/{plan_id}/steps/s1/resolve-recovery"
    )
    assert resolved.status_code == 200
    step = resolved.json()["steps"][0]
    assert step["status"] == "FAILED"
    assert step["result"]["error_code"] == "execution_uncertain"

    again = await client.post(f"/api/plans/{plan_id}/steps/s1/resolve-recovery")
    assert again.status_code == 409

    # 6. A resolved step never executes again.
    retried = await client.post(f"/api/plans/{plan_id}/steps/s1/request")
    assert retried.status_code == 409


@pytest.mark.asyncio
async def test_approval_resolution_is_single_use_through_the_api(
    client: AsyncClient, monkeypatch
):
    monkeypatch.setattr(
        default_tool_executor, "execute", _patch_timeout()
    )
    _use_provider(ScriptedProvider([DECISION_PLAN]))
    created = await client.post("/api/chat", json={"message": "Plan it"})
    clear_llm_provider_cache()
    plan_id = created.json()["plan"]["plan_id"]

    requested = await client.post(f"/api/plans/{plan_id}/steps/s1/request")
    approval_id = requested.json()["approval_id"]

    approved = await client.post(
        f"/api/approvals/{approval_id}/approve", json={"decision": "approve"}
    )
    first_status = approved.status_code
    # Approve API contract: 200 with the recorded timed-out result.
    assert first_status == 200

    replayed = await client.post(
        f"/api/approvals/{approval_id}/approve", json={"decision": "approve"}
    )
    assert replayed.status_code == 409
