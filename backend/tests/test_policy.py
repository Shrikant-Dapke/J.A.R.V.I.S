"""Tests for the policy boundary and tool invocation endpoint."""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.schemas.tool import ToolDefinition
from app.services.policy_service import evaluate_tool_policy
from app.services.system_info_tool import TOOL_NAME
from app.services.tool_executor import execute_authorized_tool
from app.services.tool_registry import ToolRegistry, tool_registry


# Test-only approval-gated definition (no implementation exists anywhere,
# so it can never execute even if policy were bypassed). Production code
# never imports this module, keeping the production registry clean.
APPROVAL_TOOL_NAME = "approval_gated_stub"

if not tool_registry.exists(APPROVAL_TOOL_NAME):
    tool_registry.register(
        ToolDefinition(
            name=APPROVAL_TOOL_NAME,
            description="Test-only approval-gated tool (never executable).",
            read_only=False,
            requires_approval=True,
        )
    )


@pytest_asyncio.fixture
async def client():
    """Create an async test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# Policy tests (isolated registries where possible)


def test_policy_allows_known_read_only_tool():
    """Registered read-only tool without approval is allowed."""
    decision = evaluate_tool_policy(TOOL_NAME)
    assert decision.allowed is True
    assert decision.requires_approval is False
    assert decision.reason


def test_policy_denies_unknown_tool():
    """Unknown tool is denied without requiring approval."""
    decision = evaluate_tool_policy("no_such_tool_xyz")
    assert decision.allowed is False
    assert decision.requires_approval is False


def test_policy_denies_approval_required_tool():
    """Approval-gated tool is denied with requires_approval set."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="gated_tool",
            description="Needs approval.",
            read_only=True,
            requires_approval=True,
        )
    )
    decision = evaluate_tool_policy("gated_tool", registry=registry)
    assert decision.allowed is False
    assert decision.requires_approval is True


def test_policy_denies_non_read_only_tool():
    """Fail-closed: non-read-only tool without approval is still denied."""
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="mutating_tool",
            description="Mutates state.",
            read_only=False,
            requires_approval=False,
        )
    )
    decision = evaluate_tool_policy("mutating_tool", registry=registry)
    assert decision.allowed is False


def test_policy_never_executes_tools():
    """Policy evaluation has no execution side effects."""
    before = execute_authorized_tool(TOOL_NAME)
    evaluate_tool_policy(TOOL_NAME)
    evaluate_tool_policy("no_such_tool_xyz")
    after = execute_authorized_tool(TOOL_NAME)
    assert before.payload == after.payload


def test_executor_rejects_unimplemented_tool():
    """Executor has no dynamic dispatch; unknown names raise ValueError."""
    with pytest.raises(ValueError):
        execute_authorized_tool("no_such_tool_xyz")
    with pytest.raises(ValueError):
        execute_authorized_tool(APPROVAL_TOOL_NAME)


# Invocation endpoint tests


@pytest.mark.asyncio
async def test_invoke_system_info_succeeds(client: AsyncClient):
    """Allowed stub executes and returns a valid ToolResult."""
    response = await client.post(f"/api/tools/{TOOL_NAME}/invoke", json={})
    assert response.status_code == 200
    data = response.json()
    assert data["tool_name"] == TOOL_NAME
    assert data["status"] == "success"
    assert data["payload"]["operating_system"]
    assert data["timestamp"]


@pytest.mark.asyncio
async def test_invoke_system_info_no_body(client: AsyncClient):
    """Bare POST without a body also succeeds (arguments optional)."""
    response = await client.post(f"/api/tools/{TOOL_NAME}/invoke")
    assert response.status_code == 200
    assert response.json()["status"] == "success"


@pytest.mark.asyncio
async def test_invoke_unknown_tool_404(client: AsyncClient):
    """Unknown tool returns structured 404."""
    response = await client.post("/api/tools/no_such_tool_xyz/invoke", json={})
    assert response.status_code == 404
    data = response.json()
    assert data["error"]["code"] == "HTTP_404"
    assert "request_id" in data["error"]


@pytest.mark.asyncio
async def test_invoke_approval_required_tool_403(client: AsyncClient):
    """Approval-gated tool returns structured 403, never executes."""
    response = await client.post(
        f"/api/tools/{APPROVAL_TOOL_NAME}/invoke", json={}
    )
    assert response.status_code == 403
    data = response.json()
    assert data["error"]["code"] == "HTTP_403"
    assert "request_id" in data["error"]


@pytest.mark.asyncio
async def test_invoke_invalid_request_422(client: AsyncClient):
    """Malformed arguments return structured 422."""
    response = await client.post(
        f"/api/tools/{TOOL_NAME}/invoke",
        json={"arguments": "not-a-dict"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_invoke_correlation_id(client: AsyncClient):
    """Correlation ID is echoed on the invoke endpoint."""
    response = await client.post(
        f"/api/tools/{TOOL_NAME}/invoke",
        json={},
        headers={"X-Request-ID": "invoke-test-1"},
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "invoke-test-1"
