"""JARVIS Backend - FastAPI Application Entry Point."""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.logging import setup_logging, get_logger


logger = get_logger(__name__)


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