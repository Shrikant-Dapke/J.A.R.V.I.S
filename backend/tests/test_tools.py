"""Tests for tool contracts, registry, and the system-info stub."""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from pydantic import ValidationError

from app.main import app
from app.schemas.tool import ToolDefinition, ToolResult
from app.services.system_info_tool import (
    TOOL_NAME,
    get_system_info,
)
from app.services.tool_registry import ToolRegistry, tool_registry


@pytest_asyncio.fixture
async def client():
    """Create an async test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# Tool contract tests


def test_tool_result_valid():
    """A well-formed tool result validates."""
    result = ToolResult(
        tool_name="get_system_info",
        status="success",
        payload={"os": "Windows"},
        error=None,
    )
    assert result.tool_name == "get_system_info"
    assert result.status == "success"
    assert result.timestamp is not None


def test_tool_result_invalid_status():
    """An unknown execution status is rejected."""
    with pytest.raises(ValidationError):
        ToolResult(tool_name="get_system_info", status="running")


def test_tool_definition_validation():
    """Tool definitions require a name, description, and flags."""
    valid = ToolDefinition(
        name="get_system_info",
        description="Stub system info.",
        read_only=True,
        requires_approval=False,
    )
    assert valid.read_only is True

    with pytest.raises(ValidationError):
        ToolDefinition(
            name="bad name!",
            description="Invalid name.",
            read_only=True,
            requires_approval=False,
        )

    with pytest.raises(ValidationError):
        ToolDefinition(
            name="get_system_info",
            description="",
            read_only=True,
            requires_approval=False,
        )


# Registry tests (isolated instance, never the global singleton)


def _definition(name: str = "demo_tool") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Demo tool.",
        read_only=True,
        requires_approval=False,
    )


def test_registry_register_and_retrieve():
    """Register then retrieve returns the same definition."""
    registry = ToolRegistry()
    registry.register(_definition("alpha_tool"))
    found = registry.get("alpha_tool")
    assert found is not None
    assert found.name == "alpha_tool"


def test_registry_list_tools():
    """List returns every registered definition."""
    registry = ToolRegistry()
    registry.register(_definition("list_one"))
    registry.register(_definition("list_two"))
    names = {d.name for d in registry.list_tools()}
    assert {"list_one", "list_two"} <= names


def test_registry_unknown_tool_lookup():
    """Unknown tool lookup returns None / False, never raises."""
    registry = ToolRegistry()
    assert registry.get("no_such_tool") is None
    assert registry.exists("no_such_tool") is False


def test_registry_duplicate_registration_rejected():
    """Duplicate registration is rejected."""
    registry = ToolRegistry()
    registry.register(_definition("dup_tool"))
    with pytest.raises(ValueError):
        registry.register(_definition("dup_tool"))


# System-info stub tests


def test_system_info_stub_deterministic():
    """Stub returns identical fixed payloads on every call."""
    first = get_system_info()
    second = get_system_info()
    assert first.status == "success"
    assert first.error is None
    assert first.payload == second.payload == {
        "os": "Windows",
        "platform": "placeholder",
        "hostname": "placeholder",
        "architecture": "x64",
    }


def test_system_info_registered_read_only():
    """Stub tool is registered globally as read-only, no approval."""
    definition = tool_registry.get(TOOL_NAME)
    assert definition is not None
    assert definition.read_only is True
    assert definition.requires_approval is False


@pytest.mark.asyncio
async def test_system_info_endpoint_schema(client: AsyncClient):
    """Endpoint returns the typed tool-result schema."""
    response = await client.get("/api/system-info")
    assert response.status_code == 200
    data = response.json()
    assert data["tool_name"] == "get_system_info"
    assert data["status"] == "success"
    assert data["payload"] == {
        "os": "Windows",
        "platform": "placeholder",
        "hostname": "placeholder",
        "architecture": "x64",
    }
    assert data["timestamp"]


@pytest.mark.asyncio
async def test_system_info_endpoint_read_only(client: AsyncClient):
    """Endpoint exposes GET only; POST is rejected."""
    response = await client.post("/api/system-info", json={})
    assert response.status_code == 405


@pytest.mark.asyncio
async def test_system_info_correlation_id(client: AsyncClient):
    """Correlation ID is echoed on the system-info endpoint."""
    response = await client.get(
        "/api/system-info", headers={"X-Request-ID": "sysinfo-test-1"}
    )
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "sysinfo-test-1"


@pytest.mark.asyncio
async def test_structured_errors_still_functional(client: AsyncClient):
    """Existing structured 404 errors still work."""
    response = await client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "HTTP_404"
