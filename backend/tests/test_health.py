"""Tests for the health endpoint."""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app


@pytest_asyncio.fixture
async def client():
    """Create an async test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_health_endpoint_returns_200(client: AsyncClient):
    """Test that the health endpoint returns HTTP 200."""
    response = await client.get("/api/health")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_endpoint_contains_application_name(client: AsyncClient):
    """Test that the health response contains the expected application name."""
    response = await client.get("/api/health")
    data = response.json()
    assert "name" in data
    assert data["name"] == "JARVIS"


@pytest.mark.asyncio
async def test_health_endpoint_status_is_healthy(client: AsyncClient):
    """Test that the health response status is 'healthy'."""
    response = await client.get("/api/health")
    data = response.json()
    assert "status" in data
    assert data["status"] == "healthy"


@pytest.mark.asyncio
async def test_health_endpoint_version_is_present(client: AsyncClient):
    """Test that the health response contains a version."""
    response = await client.get("/api/health")
    data = response.json()
    assert "version" in data
    assert isinstance(data["version"], str)
    assert len(data["version"]) > 0