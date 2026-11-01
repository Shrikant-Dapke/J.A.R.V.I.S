"""Phase 4 orchestration API tests (chat contract + plan endpoints).

Verifies the HTTP behavior of the orchestrator on POST /api/chat and
the new plan endpoints: structured proposals, rejections, approvals,
session ownership, and provider failure mapping.
"""

import uuid
from unittest.mock import Mock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.llm import LLMResponse
from app.schemas.policy import RiskLevel
from app.schemas.tool import ToolDefinition
from app.services import open_application_tool
from app.services.llm.base import LLMProvider
from app.services.llm.errors import LLMProviderError, LLMTimeoutError
from app.services.llm.factory import (
    clear_llm_provider_cache,
    set_llm_provider_override,
)
from app.services.system_info_tool import SystemInfoTool
from app.services.tool_registry import tool_registry


DECISION_REPLY = '{"action": "reply", "reply": "Conversational answer."}'
DECISION_SYSTEM_INFO = (
    '{"action": "tool_action", "tool": "get_system_info", '
    '"arguments": {}, "explanation": "Reading system info."}'
)
DECISION_OPEN_NOTEPAD = (
    '{"action": "tool_action", "tool": "open_application", '
    '"arguments": {"app_id": "notepad"}, "explanation": "Opening notepad."}'
)
DECISION_UNKNOWN_TOOL = (
    '{"action": "tool_action", "tool": "format_hard_drive", '
    '"arguments": {}}'
)
DECISION_MALFORMED = '{"action": "tool_action", "tool": "bad id!", "args": 1}'
DECISION_PLAN = (
    '{"action": "plan", "goal": "Info then notepad", '
    '"explanation": "Need two actions.", "steps": ['
    '{"step_id": "s1", "description": "Get system info", '
    '"tool_name": "get_system_info", "arguments": {}, "depends_on": []}, '
    '{"step_id": "s2", "description": "Open notepad", '
    '"tool_name": "open_application", "arguments": {"app_id": "notepad"}, '
    '"depends_on": ["s1"]}]}'
)
SUMMARY_TEXT = "Tool finished."

# Test-only stub tool registered against the GLOBAL registry (same pattern
# the other phase-4 modules use): read-only metadata with no executable
# implementation, so it must fail closed everywhere.
STUB_READ_ONLY_TOOL = "stub_readonly_tool"

if not tool_registry.exists(STUB_READ_ONLY_TOOL):
    tool_registry.register(
        ToolDefinition(
            name=STUB_READ_ONLY_TOOL,
            description="Test-only read-only metadata stub (no implementation).",
            read_only=True,
            requires_approval=False,
        )
    )

# Test-only registered-deny tool (this module must be independent).
DENIED_TOOL_NAME = "chat_denied_stub"

if not tool_registry.exists(DENIED_TOOL_NAME):
    tool_registry.register(
        ToolDefinition(
            name=DENIED_TOOL_NAME,
            description="Test-only policy-denied tool (never executable).",
            read_only=False,
            requires_approval=False,
        )
    )

# Test-only tool with an UNSAFE policy classification: claims read-only
# risk while mutating, so policy hard-denies it WITHOUT approval
# requestability — the true ApprovalWorkflowError (403) denial branch.
UNSAFE_TOOL_NAME = "stub_unsafe_classification"

if not tool_registry.exists(UNSAFE_TOOL_NAME):
    tool_registry.register(
        ToolDefinition(
            name=UNSAFE_TOOL_NAME,
            description="Test-only tool with an unsafe policy classification.",
            read_only=False,
            requires_approval=False,
            risk_level=RiskLevel.READ_ONLY,
        )
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


@pytest.mark.asyncio
async def test_chat_plain_output_stays_conversational(client: AsyncClient):
    """Without a decision object the chat contract is unchanged."""
    _use_provider(ScriptedProvider(["JARVIS assistant core is online."]))

    response = await client.post("/api/chat", json={"message": "Hello"})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["response"] == "JARVIS assistant core is online."
    uuid.UUID(data["conversation_id"])


@pytest.mark.asyncio
async def test_chat_structured_reply(client: AsyncClient):
    """A valid reply decision flows through the chat contract."""
    _use_provider(ScriptedProvider([DECISION_REPLY]))

    response = await client.post("/api/chat", json={"message": "Hello"})

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["response"] == "Conversational answer."


@pytest.mark.asyncio
async def test_chat_read_only_tool_executes_and_summarizes(
    client: AsyncClient,
):
    """Policy-allowed read-only tools execute once and are summarized."""
    executions = []
    original_execute = SystemInfoTool.execute

    def counting_execute(self, arguments):
        executions.append(arguments)
        return original_execute(self, arguments)

    with patch.object(SystemInfoTool, "execute", counting_execute):
        _use_provider(ScriptedProvider([DECISION_SYSTEM_INFO, SUMMARY_TEXT]))

        response = await client.post(
            "/api/chat", json={"message": "Show system info"}
        )
    clear_llm_provider_cache()

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["response"] == SUMMARY_TEXT
    assert data["tool_result"]["tool_name"] == "get_system_info"
    assert data["tool_result"]["success"] is True
    assert len(executions) == 1


@pytest.mark.asyncio
async def test_chat_mutating_tool_proposes_approval(client: AsyncClient, monkeypatch):
    """Mutating proposals return the exact pending approval, 200, no run."""
    popen = Mock()
    monkeypatch.setattr(open_application_tool, "WINDOWS_PLATFORM", True)
    monkeypatch.setattr(open_application_tool.subprocess, "Popen", popen)
    _use_provider(ScriptedProvider([DECISION_OPEN_NOTEPAD]))

    response = await client.post("/api/chat", json={"message": "Open notepad"})
    clear_llm_provider_cache()

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "action_proposed"
    assert data["tool_result"] is None
    action = data["action"]
    assert action["tool_name"] == "open_application"
    assert action["arguments"] == {"app_id": "notepad"}
    assert action["approval_status"] == "PENDING"
    popen.assert_not_called()

    # The approval endpoint executes the exact approved action once.
    approval_id = action["approval_id"]
    approved = await client.post(f"/api/approvals/{approval_id}/approve")
    replay = await client.post(f"/api/approvals/{approval_id}/approve")

    assert approved.status_code == 200
    assert approved.json()["status"] == "success"
    assert replay.status_code == 409
    popen.assert_called_once_with(
        [open_application_tool.APPLICATION_ALLOWLIST["notepad"]], shell=False
    )


@pytest.mark.asyncio
async def test_chat_unknown_tool_rejected_422(client: AsyncClient):
    """A proposal naming an unregistered tool is rejected, nothing runs."""
    _use_provider(ScriptedProvider([DECISION_UNKNOWN_TOOL]))

    response = await client.post("/api/chat", json={"message": "Format it"})

    clear_llm_provider_cache()
    assert response.status_code == 422
    data = response.json()
    assert data["error"]["code"] == "HTTP_422"
    assert "request_id" in data["error"]
    # Raw model output never appears in the error body.
    assert "format_hard_drive" not in response.text


@pytest.mark.asyncio
async def test_chat_malformed_decision_rejected_422(client: AsyncClient):
    """Decision-shaped malformed output fails closed with 422."""
    _use_provider(ScriptedProvider([DECISION_MALFORMED]))

    response = await client.post("/api/chat", json={"message": "Do it"})

    clear_llm_provider_cache()
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "HTTP_422"


@pytest.mark.asyncio
async def test_chat_provider_failure_maps_to_502(client: AsyncClient):
    """Provider transport failures keep the established 502 mapping."""
    _use_provider(ScriptedProvider([LLMProviderError("boom")]))

    response = await client.post("/api/chat", json={"message": "Hello"})

    clear_llm_provider_cache()
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "HTTP_502"


@pytest.mark.asyncio
async def test_chat_provider_timeout_maps_to_504(client: AsyncClient):
    """Provider timeouts keep the established 504 mapping."""
    _use_provider(ScriptedProvider([LLMTimeoutError("slow")]))

    response = await client.post("/api/chat", json={"message": "Hello"})

    clear_llm_provider_cache()
    assert response.status_code == 504


@pytest.mark.asyncio
async def test_chat_plan_proposal_captured(client: AsyncClient):
    """A plan decision is validated, returned, and never executed."""
    _use_provider(ScriptedProvider([DECISION_PLAN]))

    response = await client.post(
        "/api/chat", json={"message": "Show info then open notepad"}
    )

    clear_llm_provider_cache()
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "plan_proposed"
    assert data["plan"]["goal"] == "Info then notepad"
    assert len(data["plan"]["steps"]) == 2
    assert data["plan"]["steps"][1]["depends_on"] == ["s1"]
    assert data["action"] is None

    plan_id = data["plan"]["plan_id"]
    stored = await client.get(f"/api/plans/{plan_id}")
    assert stored.status_code == 200
    assert stored.json()["plan_id"] == plan_id
    assert stored.json()["steps"][0]["status"] == "PENDING"


@pytest.mark.asyncio
async def test_plan_foreign_session_cannot_read(client: AsyncClient, other_session_headers):
    """Plans are owner/session scoped exactly like conversations."""
    _use_provider(ScriptedProvider([DECISION_PLAN]))
    created = await client.post("/api/chat", json={"message": "Plan it"})
    clear_llm_provider_cache()
    plan_id = created.json()["plan"]["plan_id"]

    foreign = await client.get(
        f"/api/plans/{plan_id}",
        headers={
            "Authorization": other_session_headers["Authorization"],
            "X-JARVIS-Session": other_session_headers["X-JARVIS-Session"],
        },
    )

    assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_plan_step_request_executes_ready_read_only_step(client: AsyncClient):
    """Explicit step execution resolves the step and nothing else."""
    _use_provider(ScriptedProvider([DECISION_PLAN]))
    created = await client.post("/api/chat", json={"message": "Plan it"})
    clear_llm_provider_cache()
    plan_id = created.json()["plan"]["plan_id"]

    response = await client.post(
        f"/api/plans/{plan_id}/steps/s1/request"
    )

    assert response.status_code == 200
    data = response.json()
    assert data["tool_name"] == "get_system_info"
    assert data["success"] is True

    plan = await client.get(f"/api/plans/{plan_id}")
    assert plan.json()["steps"][0]["status"] == "COMPLETED"
    assert plan.json()["steps"][1]["status"] == "PENDING"
    # s2 depends on s1 but is still pending: only s1 was executed.
    blocking = await client.post(f"/api/plans/{plan_id}/steps/s2/request")
    # s2 is mutating: it returns a pending approval (202), never executes.
    assert blocking.status_code == 202
    assert blocking.json()["status"] == "PENDING"


@pytest.mark.asyncio
async def test_plan_step_unready_is_conflict(client: AsyncClient):
    """A step whose dependencies are not complete cannot be requested."""
    _use_provider(ScriptedProvider([DECISION_PLAN]))
    created = await client.post("/api/chat", json={"message": "Plan it"})
    clear_llm_provider_cache()
    plan_id = created.json()["plan"]["plan_id"]

    # s2 depends on s1, which is still PENDING: conflict, nothing runs.
    unready = await client.post(f"/api/plans/{plan_id}/steps/s2/request")
    assert unready.status_code == 409

    unknown_step = await client.post(f"/api/plans/{plan_id}/steps/q_no/request")
    assert unknown_step.status_code == 404

    # After s1 executes, s2 becomes ready and returns a pending approval.
    first = await client.post(f"/api/plans/{plan_id}/steps/s1/request")
    assert first.status_code == 200
    ready = await client.post(f"/api/plans/{plan_id}/steps/s2/request")
    assert ready.status_code == 202
    assert ready.json()["status"] == "PENDING"


@pytest.mark.asyncio
async def test_plan_step_foreign_session_cannot_request(client: AsyncClient, other_session_headers):
    """Step execution requires the owning session."""
    _use_provider(ScriptedProvider([DECISION_PLAN]))
    created = await client.post("/api/chat", json={"message": "Plan it"})
    clear_llm_provider_cache()
    plan_id = created.json()["plan"]["plan_id"]

    foreign = await client.post(
        f"/api/plans/{plan_id}/steps/s1/request",
        headers={
            "Authorization": other_session_headers["Authorization"],
            "X-JARVIS-Session": other_session_headers["X-JARVIS-Session"],
        },
    )

    assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_plan_endpoints_require_authentication():
    """Unauthenticated plan requests fail closed with 401."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as unauthenticated:
        response = await unauthenticated.get("/api/plans/whatever")
        step = await unauthenticated.post("/api/plans/whatever/steps/s1/request")

    assert response.status_code == 401
    assert step.status_code == 401


@pytest.mark.asyncio
async def test_chat_proposal_cannot_be_approved_by_other_session(
    client: AsyncClient, other_session_headers, monkeypatch
):
    """Chat-created proposals obey the same session binding as tools."""
    popen = Mock()
    monkeypatch.setattr(open_application_tool, "WINDOWS_PLATFORM", True)
    monkeypatch.setattr(open_application_tool.subprocess, "Popen", popen)
    _use_provider(ScriptedProvider([DECISION_OPEN_NOTEPAD]))
    created = await client.post("/api/chat", json={"message": "Open notepad"})
    clear_llm_provider_cache()
    approval_id = created.json()["action"]["approval_id"]

    foreign = await client.post(
        f"/api/approvals/{approval_id}/approve",
        headers={
            "Authorization": other_session_headers["Authorization"],
            "X-JARVIS-Session": other_session_headers["X-JARVIS-Session"],
        },
    )

    assert foreign.status_code == 404
    popen.assert_not_called()


# --- unimplemented registered tools ----------------------------------------


@pytest.mark.asyncio
async def test_chat_unimplemented_tool_maps_to_422(client: AsyncClient):
    """A read-only stub proposal is rejected safely, not a 500."""
    _use_provider(
        ScriptedProvider(
            [
                '{"action": "tool_action", "tool": "stub_readonly_tool", '
                '"arguments": {}}'
            ]
        )
    )

    response = await client.post("/api/chat", json={"message": "Use the stub"})

    clear_llm_provider_cache()
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "HTTP_422"
    assert "stub_readonly_tool" not in response.text


@pytest.mark.asyncio
async def test_plan_unimplemented_readonly_step_records_failure(
    client: AsyncClient,
):
    """A stub plan step resolves FAILED honestly via the plan endpoint."""
    plan_decision = (
        '{"action": "plan", "goal": "Use stub", "steps": ['
        '{"step_id": "s1", "description": "Run stub", '
        '"tool_name": "stub_readonly_tool", "arguments": {}, '
        '"depends_on": []}]}'
    )
    _use_provider(ScriptedProvider([plan_decision]))
    created = await client.post("/api/chat", json={"message": "Do the stub"})
    clear_llm_provider_cache()
    plan_id = created.json()["plan"]["plan_id"]

    response = await client.post(
        f"/api/plans/{plan_id}/steps/s1/request"
    )

    assert response.status_code == 200
    data = response.json()
    assert data["tool_name"] == "stub_readonly_tool"
    assert data["success"] is False
    assert data["error_code"] == "tool_unavailable"

    plan = await client.get(f"/api/plans/{plan_id}")
    assert plan.json()["steps"][0]["status"] == "FAILED"


# --- oversized message budget ----------------------------------------------


@pytest.mark.asyncio
async def test_oversized_message_maps_to_422_without_truncation(
    client: AsyncClient,
):
    """Requests beyond the instruction budget fail clearly."""
    _use_provider(ScriptedProvider([]))

    response = await client.post(
        "/api/chat", json={"message": "x" * 1501}
    )

    clear_llm_provider_cache()
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "HTTP_422"
    assert response.json()["error"]["message"] == (
        "The message exceeds the orchestrator instruction budget."
    )


# --- plan step policy denial -------------------------------------------------


@pytest.mark.asyncio
async def test_plan_step_with_denied_tool_maps_to_403(client: AsyncClient):
    """A policy-denied step stays pending, never 500.

    The stub is read-only metadata classified unsafe by policy, so the
    approval workflow hard-denies it without approval requestability.
    """
    denied_plan = (
        '{"action": "plan", "goal": "Impossible step", "steps": ['
        '{"step_id": "s1", "description": "Run denied tool", '
        '"tool_name": "stub_unsafe_classification", "arguments": {}, '
        '"depends_on": []}]}'
    )
    _use_provider(ScriptedProvider([denied_plan]))
    created = await client.post("/api/chat", json={"message": "Do it"})
    clear_llm_provider_cache()
    plan_id = created.json()["plan"]["plan_id"]

    response = await client.post(
        f"/api/plans/{plan_id}/steps/s1/request"
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "HTTP_403"

    # Policy rejections do not burn the step (nothing executed).
    plan = await client.get(f"/api/plans/{plan_id}")
    assert plan.json()["steps"][0]["status"] == "PENDING"


# --- safe-origin requirement on the plan execution endpoint ------------------


@pytest.mark.asyncio
async def test_plan_step_request_rejects_unsafe_origin(client: AsyncClient):
    """Browser origins must be configured before requesting step execution."""
    _use_provider(ScriptedProvider([DECISION_PLAN]))
    created = await client.post("/api/chat", json={"message": "Plan it"})
    clear_llm_provider_cache()
    plan_id = created.json()["plan"]["plan_id"]

    forbidden = await client.post(
        f"/api/plans/{plan_id}/steps/s1/request",
        headers={"Origin": "https://attacker.example"},
    )
    assert forbidden.status_code == 403

    # Native clients (no Origin header) remain supported.
    native = await client.post(
        f"/api/plans/{plan_id}/steps/s1/request"
    )
    assert native.status_code == 200
    assert native.json()["tool_name"] == "get_system_info"
