"""Phase 3 policy and approval workflow tests."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.main import app
from app.schemas.approval import ApprovalRequest, ApprovalStatus
from app.schemas.policy import RiskLevel
from app.schemas.tool import ToolDefinition
from app.services.approval_service import ApprovalPolicyError, ApprovalService
from app.services.approval_store import (
    ApprovalStateError,
    ApprovalStore,
)
from app.services import open_application_tool
from app.services.policy_service import evaluate_tool_policy
from app.services.tool_executor import ToolExecutor
from app.services.tool_registry import ToolRegistry, tool_registry


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _service(clock=None, ttl_seconds=300):
    store = ApprovalStore(ttl_seconds=ttl_seconds, clock=clock)
    return ApprovalService(
        registry=tool_registry,
        executor=ToolExecutor(tool_registry),
        store=store,
    ), store


def test_policy_classifies_builtin_tools():
    system_info = evaluate_tool_policy("get_system_info")
    open_app = evaluate_tool_policy("open_application")
    create_directory = evaluate_tool_policy("create_directory")

    assert system_info.allowed is True
    assert system_info.risk_level is RiskLevel.READ_ONLY
    assert open_app.allowed is False
    assert open_app.requires_approval is True
    assert open_app.risk_level is RiskLevel.LOW_RISK
    assert create_directory.requires_approval is True
    assert create_directory.risk_level is RiskLevel.LOW_RISK


def test_policy_requires_approval_for_high_risk_tools():
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="high_risk_test",
            description="Test high-risk action.",
            read_only=False,
            requires_approval=False,
            risk_level=RiskLevel.HIGH_RISK,
        )
    )

    decision = evaluate_tool_policy("high_risk_test", registry=registry)

    assert decision.allowed is False
    assert decision.requires_approval is True
    assert decision.risk_level is RiskLevel.HIGH_RISK


def test_policy_never_allows_unknown_tool():
    decision = evaluate_tool_policy("unknown_phase3_tool")

    assert decision.allowed is False
    assert decision.requires_approval is False
    assert decision.risk_level is None


def test_approval_store_create_and_retrieve():
    store = ApprovalStore()
    request = store.create_approval_request(
        tool_name="open_application",
        arguments={"app_id": "notepad"},
        action_description="Open notepad.",
        risk_level=RiskLevel.LOW_RISK,
    )

    retrieved = store.get_approval_request(request.approval_id)

    assert retrieved.approval_id == request.approval_id
    assert retrieved.status is ApprovalStatus.PENDING
    assert retrieved.arguments == {"app_id": "notepad"}
    assert retrieved.expires_at > retrieved.created_at


def test_approval_store_approve_and_single_use():
    service, store = _service()
    request = service.request_tool("open_application", {"app_id": "notepad"})
    assert isinstance(request, ApprovalRequest)

    approved = store.approve_request(request.approval_id)
    consumed = store.claim_approved_for_execution(request.approval_id)

    assert approved.status is ApprovalStatus.APPROVED
    assert consumed.status is ApprovalStatus.EXECUTED
    with pytest.raises(ApprovalStateError):
        store.claim_approved_for_execution(request.approval_id)


def test_rejected_approval_never_executes(monkeypatch):
    service, store = _service()
    execute = Mock()
    monkeypatch.setattr(service._executor, "execute", execute)
    request = service.request_tool("open_application", {"app_id": "notepad"})

    rejected = service.reject_request(request.approval_id)

    assert rejected.status is ApprovalStatus.REJECTED
    with pytest.raises(ApprovalStateError):
        service.approve_request(request.approval_id)
    execute.assert_not_called()


def test_cannot_reject_approved_request():
    service, store = _service()
    request = service.request_tool("open_application", {"app_id": "notepad"})
    store.approve_request(request.approval_id)

    with pytest.raises(ApprovalStateError):
        service.reject_request(request.approval_id)


def test_expiration_prevents_approval_and_execution():
    current = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    service, store = _service(clock=lambda: current[0], ttl_seconds=5)
    request = service.request_tool("open_application", {"app_id": "notepad"})
    current[0] += timedelta(seconds=6)

    expired = service.get_request(request.approval_id)

    assert expired.status is ApprovalStatus.EXPIRED
    with pytest.raises(ApprovalStateError):
        service.approve_request(request.approval_id)
    with pytest.raises(ApprovalStateError):
        store.claim_approved_for_execution(request.approval_id)


def test_approved_request_also_expires_before_consumption():
    current = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    service, store = _service(clock=lambda: current[0], ttl_seconds=5)
    request = service.request_tool("open_application", {"app_id": "notepad"})
    store.approve_request(request.approval_id)
    current[0] += timedelta(seconds=6)

    with pytest.raises(ApprovalStateError):
        store.claim_approved_for_execution(request.approval_id)
    assert store.get_approval_request(request.approval_id).status is ApprovalStatus.EXPIRED


def test_pending_request_never_reaches_executor():
    service, _ = _service()
    execute = Mock()
    service._executor.execute = execute

    request = service.request_tool("open_application", {"app_id": "notepad"})

    assert isinstance(request, ApprovalRequest)
    execute.assert_not_called()


def test_approval_copy_cannot_tamper_with_original_arguments():
    service, store = _service()
    request = service.request_tool("open_application", {"app_id": "notepad"})
    request.arguments["app_id"] = "powershell"

    stored = store.get_approval_request(request.approval_id)

    assert stored.arguments == {"app_id": "notepad"}


def test_secret_like_arguments_are_not_stored():
    with pytest.raises(ValidationError):
        ApprovalRequest(
            approval_id="approval-1",
            tool_name="test_tool",
            arguments={"api_key": "must-not-store"},
            action_description="Test.",
            risk_level=RiskLevel.LOW_RISK,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )


def test_approval_policy_is_rechecked_before_execution():
    service, store = _service()
    request = service.request_tool("open_application", {"app_id": "notepad"})
    store._requests[request.approval_id] = ApprovalRequest(
        **{
            **store.get_approval_request(request.approval_id).model_dump(),
            "risk_level": RiskLevel.HIGH_RISK,
        }
    )

    with pytest.raises(ApprovalPolicyError):
        service.approve_request(request.approval_id)


@pytest.mark.asyncio
async def test_invocation_returns_pending_approval_without_launching(
    client: AsyncClient, monkeypatch
):
    popen = Mock()
    monkeypatch.setattr(open_application_tool, "WINDOWS_PLATFORM", True)
    monkeypatch.setattr(open_application_tool.subprocess, "Popen", popen)

    response = await client.post(
        "/api/tools/open_application/invoke",
        json={"arguments": {"app_id": "notepad"}},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "PENDING"
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_approval_api_approve_executes_once(client: AsyncClient, monkeypatch):
    popen = Mock()
    monkeypatch.setattr(open_application_tool, "WINDOWS_PLATFORM", True)
    monkeypatch.setattr(open_application_tool.subprocess, "Popen", popen)
    requested = await client.post(
        "/api/tools/open_application/invoke",
        json={"arguments": {"app_id": "notepad"}},
    )
    approval_id = requested.json()["approval_id"]

    approved = await client.post(f"/api/approvals/{approval_id}/approve")
    replay = await client.post(f"/api/approvals/{approval_id}/approve")
    state = await client.get(f"/api/approvals/{approval_id}")

    assert approved.status_code == 200
    assert approved.json()["status"] == "success"
    assert replay.status_code == 409
    assert state.json()["status"] == "EXECUTED"
    popen.assert_called_once_with(["notepad.exe"], shell=False)


@pytest.mark.asyncio
async def test_approval_api_reject_never_executes(client: AsyncClient, monkeypatch):
    popen = Mock()
    monkeypatch.setattr(open_application_tool, "WINDOWS_PLATFORM", True)
    monkeypatch.setattr(open_application_tool.subprocess, "Popen", popen)
    requested = await client.post(
        "/api/tools/open_application/invoke",
        json={"arguments": {"app_id": "notepad"}},
    )
    approval_id = requested.json()["approval_id"]

    rejected = await client.post(f"/api/approvals/{approval_id}/reject")
    approved = await client.post(f"/api/approvals/{approval_id}/approve")

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    assert approved.status_code == 409
    popen.assert_not_called()


@pytest.mark.asyncio
async def test_approval_api_ignores_argument_changes_from_approver(
    client: AsyncClient, monkeypatch
):
    popen = Mock()
    monkeypatch.setattr(open_application_tool, "WINDOWS_PLATFORM", True)
    monkeypatch.setattr(open_application_tool.subprocess, "Popen", popen)
    requested = await client.post(
        "/api/tools/open_application/invoke",
        json={"arguments": {"app_id": "notepad"}},
    )
    approval_id = requested.json()["approval_id"]

    approved = await client.post(
        f"/api/approvals/{approval_id}/approve",
        json={"arguments": {"app_id": "powershell"}},
    )

    assert approved.status_code == 200
    popen.assert_called_once_with(["notepad.exe"], shell=False)


@pytest.mark.asyncio
async def test_approval_api_unknown_request_is_404(client: AsyncClient):
    response = await client.get("/api/approvals/does-not-exist")

    assert response.status_code == 404
