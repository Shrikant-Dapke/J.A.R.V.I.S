"""Authentication and origin checks for the local API boundary."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
from dataclasses import dataclass
from typing import Optional

from fastapi import Header, Request, status
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings


API_TOKEN_HEADER = "X-JARVIS-Token"
SESSION_HEADER = "X-JARVIS-Session"
_SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


@dataclass(frozen=True)
class AuthenticatedSession:
    """Authenticated API owner plus an explicit client session identifier."""

    owner_id: str
    session_id: str


def allowed_origins() -> list[str]:
    """Return configured exact browser origins, excluding empty entries."""
    return [
        origin.strip().rstrip("/")
        for origin in settings.allowed_origins.split(",")
        if origin.strip()
    ]


def is_loopback_host(value: Optional[str]) -> bool:
    """Return whether a literal host is a loopback IP address."""
    if not value:
        return False
    try:
        return ipaddress.ip_address(value.strip("[]")).is_loopback
    except ValueError:
        return False


def _provided_token(
    authorization: Optional[str],
    header_token: Optional[str],
) -> Optional[str]:
    """Read one bearer token from supported local-client headers."""
    bearer_token = None
    if authorization:
        scheme, separator, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not value.strip():
            raise StarletteHTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication is required.",
            )
        bearer_token = value.strip()

    if bearer_token and header_token and not secrets.compare_digest(
        bearer_token, header_token
    ):
        raise StarletteHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
        )
    return bearer_token or (header_token.strip() if header_token else None)


def require_authenticated_session(
    authorization: Optional[str] = Header(default=None),
    header_token: Optional[str] = Header(default=None, alias=API_TOKEN_HEADER),
    session_id: Optional[str] = Header(default=None, alias=SESSION_HEADER),
) -> AuthenticatedSession:
    """Authenticate a local client and require an explicit session boundary."""
    if settings.api_token is None:
        raise StarletteHTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API authentication is not configured.",
        )

    provided = _provided_token(authorization, header_token)
    expected = settings.api_token.get_secret_value()
    if not provided or not secrets.compare_digest(provided, expected):
        raise StarletteHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
        )

    if not session_id or not _SESSION_PATTERN.fullmatch(session_id):
        raise StarletteHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="An authenticated client session is required.",
        )

    owner_id = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    return AuthenticatedSession(owner_id=owner_id, session_id=session_id)


def require_safe_origin(request: Request) -> None:
    """Reject state-changing browser requests from unconfigured origins."""
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in allowed_origins():
        raise StarletteHTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The request origin is not allowed.",
        )
