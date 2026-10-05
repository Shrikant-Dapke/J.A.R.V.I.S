"""JARVIS Backend - FastAPI Application Entry Point."""

import time
import uuid
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.chat import router as chat_router
from app.api.conversations import router as conversations_router
from app.api.system_info import router as system_info_router
from app.api.tools import router as tools_router
from app.core.config import settings
from app.core.logging import setup_logging, get_logger


logger = get_logger(__name__)


CORRELATION_ID_HEADER = "X-Request-ID"
MAX_CORRELATION_ID_LENGTH = 128


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    setup_logging()
    logger.info("Application starting", extra={"extra_fields": {"app_name": settings.app_name, "version": settings.app_version}})
    yield
    # Shutdown
    logger.info("Application shutting down")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="JARVIS - Just A Rather Very Intelligent System",
    lifespan=lifespan,
)


def _validate_correlation_id(value: str) -> str:
    """Validate and sanitize correlation ID."""
    if not value:
        return str(uuid.uuid4())
    value = value.strip()
    if len(value) > MAX_CORRELATION_ID_LENGTH:
        return str(uuid.uuid4())
    # Allow alphanumeric, hyphen, underscore
    import re
    if not re.match(r'^[a-zA-Z0-9_-]+$', value):
        return str(uuid.uuid4())
    return value


def _sanitize_for_json(obj):
    """Recursively convert non-JSON-serializable values to strings."""
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _create_error_response(
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    details: dict = None,
) -> JSONResponse:
    """Create structured error response."""
    correlation_id = getattr(request.state, "correlation_id", None)
    content = {
        "error": {
            "code": error_code,
            "message": message,
        }
    }
    if details:
        content["error"]["details"] = details
    if correlation_id:
        content["error"]["request_id"] = correlation_id
    return JSONResponse(content=content, status_code=status_code)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next: Callable):
    """Extract or generate correlation ID and attach to request state."""
    incoming_id = request.headers.get(CORRELATION_ID_HEADER)
    correlation_id = _validate_correlation_id(incoming_id)
    request.state.correlation_id = correlation_id

    response = await call_next(request)

    response.headers[CORRELATION_ID_HEADER] = correlation_id
    return response


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next: Callable):
    """Log HTTP requests with method, path, status, and duration."""
    start_time = time.perf_counter()
    correlation_id = getattr(request.state, "correlation_id", None)

    try:
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            "HTTP request completed",
            extra={
                "extra_fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round(duration_ms, 2),
                    "correlation_id": correlation_id,
                }
            },
        )
        return response
    except Exception:
        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.exception(
            "HTTP request failed",
            extra={
                "extra_fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round(duration_ms, 2),
                    "correlation_id": correlation_id,
                }
            },
        )
        raise


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Handle HTTP exceptions with structured responses."""
    return _create_error_response(
        request=request,
        status_code=exc.status_code,
        error_code=f"HTTP_{exc.status_code}",
        message=exc.detail,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle request validation errors with structured responses."""
    return _create_error_response(
        request=request,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        error_code="VALIDATION_ERROR",
        message="Request validation failed",
        details={"errors": _sanitize_for_json(exc.errors())},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Handle unexpected exceptions without exposing internals."""
    correlation_id = getattr(request.state, "correlation_id", None)
    logger.exception(
        "Unhandled exception",
        extra={"extra_fields": {"correlation_id": correlation_id, "path": request.url.path}},
    )
    return _create_error_response(
        request=request,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        error_code="INTERNAL_ERROR",
        message="An unexpected error occurred",
    )


app.include_router(chat_router)
app.include_router(conversations_router)
app.include_router(system_info_router)
app.include_router(tools_router)


@app.get("/api/health", tags=["health"])
async def health_check() -> JSONResponse:
    """
    Health check endpoint.

    Returns:
        JSON response with application name, status, and version.
    """
    return JSONResponse(
        content={
            "name": settings.app_name,
            "status": "healthy",
            "version": settings.app_version,
        },
        status_code=200,
    )


@app.get("/", tags=["root"])
async def root() -> JSONResponse:
    """Root endpoint."""
    return JSONResponse(
        content={
            "name": settings.app_name,
            "version": settings.app_version,
            "message": "JARVIS Backend API",
        },
        status_code=200,
    )