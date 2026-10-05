"""Tests for the health endpoint and API foundation."""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.config import Settings


@pytest_asyncio.fixture
async def client():
    """Create an async test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# Health endpoint tests


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


# Correlation ID tests


@pytest.mark.asyncio
async def test_health_endpoint_returns_request_id_header(client: AsyncClient):
    """Test that the health endpoint returns X-Request-ID header."""
    response = await client.get("/api/health")
    assert "X-Request-ID" in response.headers
    request_id = response.headers["X-Request-ID"]
    assert isinstance(request_id, str)
    assert len(request_id) > 0


@pytest.mark.asyncio
async def test_health_endpoint_echoes_valid_request_id(client: AsyncClient):
    """Test that a valid X-Request-ID is echoed back."""
    test_id = "test-request-123"
    response = await client.get("/api/health", headers={"X-Request-ID": test_id})
    assert response.headers["X-Request-ID"] == test_id


@pytest.mark.asyncio
async def test_health_endpoint_generates_request_id_when_absent(client: AsyncClient):
    """Test that a request ID is generated when not provided."""
    response = await client.get("/api/health")
    request_id = response.headers["X-Request-ID"]
    # Should be a valid UUID format
    import uuid
    try:
        uuid.UUID(request_id)
    except ValueError:
        pytest.fail(f"Generated request ID is not a valid UUID: {request_id}")


@pytest.mark.asyncio
async def test_health_endpoint_rejects_invalid_request_id(client: AsyncClient):
    """Test that invalid X-Request-ID is rejected and a new one generated."""
    # Test with special characters
    response = await client.get("/api/health", headers={"X-Request-ID": "invalid@id!"})
    request_id = response.headers["X-Request-ID"]
    assert request_id != "invalid@id!"
    # Should be a valid UUID
    import uuid
    uuid.UUID(request_id)


@pytest.mark.asyncio
async def test_health_endpoint_rejects_excessively_long_request_id(client: AsyncClient):
    """Test that excessively long X-Request-ID is rejected."""
    long_id = "a" * 200
    response = await client.get("/api/health", headers={"X-Request-ID": long_id})
    request_id = response.headers["X-Request-ID"]
    assert request_id != long_id
    assert len(request_id) <= 128


# Root endpoint tests


@pytest.mark.asyncio
async def test_root_endpoint_returns_200(client: AsyncClient):
    """Test that the root endpoint returns HTTP 200."""
    response = await client.get("/")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_root_endpoint_contains_expected_fields(client: AsyncClient):
    """Test that the root response contains expected fields."""
    response = await client.get("/")
    data = response.json()
    assert data["name"] == "JARVIS"
    assert data["version"] == "0.1.0"
    assert data["message"] == "JARVIS Backend API"


@pytest.mark.asyncio
async def test_root_endpoint_returns_request_id_header(client: AsyncClient):
    """Test that the root endpoint returns X-Request-ID header."""
    response = await client.get("/")
    assert "X-Request-ID" in response.headers


# Error handling tests


@pytest.mark.asyncio
async def test_404_returns_structured_error(client: AsyncClient):
    """Test that 404 returns structured error response."""
    response = await client.get("/api/nonexistent")
    assert response.status_code == 404
    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == "HTTP_404"
    assert "request_id" in data["error"]


@pytest.mark.asyncio
async def test_validation_error_returns_structured_error(client: AsyncClient):
    """Test that validator-based 422s return structured JSON responses."""
    # Triggers a Pydantic field_validator ValueError (covers the
    # _sanitize_for_json path for non-serializable ctx values).
    response = await client.post("/api/chat", json={"message": "   "})
    assert response.status_code == 422
    data = response.json()
    assert data["error"]["code"] == "VALIDATION_ERROR"
    assert "request_id" in data["error"]
    assert "errors" in data["error"]["details"]


# Configuration validation tests


def test_config_valid_app_env():
    """Test that valid app_env values are accepted."""
    for env in ["development", "staging", "production", "DEVELOPMENT", "Staging"]:
        settings = Settings(app_env=env)
        assert settings.app_env in {"development", "staging", "production"}


def test_config_invalid_app_env():
    """Test that invalid app_env values are rejected."""
    with pytest.raises(ValueError) as exc_info:
        Settings(app_env="invalid")
    assert "app_env must be one of" in str(exc_info.value)


def test_config_valid_log_level():
    """Test that valid log_level values are accepted."""
    for level in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "debug", "info"]:
        settings = Settings(log_level=level)
        assert settings.log_level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def test_config_invalid_log_level():
    """Test that invalid log_level values are rejected."""
    with pytest.raises(ValueError) as exc_info:
        Settings(log_level="INVALID")
    assert "log_level must be one of" in str(exc_info.value)


def test_config_valid_log_format():
    """Test that valid log_format values are accepted."""
    for fmt in ["json", "text", "JSON", "Text"]:
        settings = Settings(log_format=fmt)
        assert settings.log_format in {"json", "text"}


def test_config_invalid_log_format():
    """Test that invalid log_format values are rejected."""
    with pytest.raises(ValueError) as exc_info:
        Settings(log_format="xml")
    assert "log_format must be one of" in str(exc_info.value)