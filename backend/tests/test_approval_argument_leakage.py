"""Production action-description safety (Phase 5.3, FIX B).

``action_description`` reaches the metadata-only approval inventory and
chat responses, so the production builder may never embed argument
values into it. These tests exercise the REAL production creation path
(``ApprovalService.request_tool`` → registry static description →
approval store → inventory serialization) with distinctive sensitive
markers and prove:

- argument values (flat, token-shaped, and stringified nested
  structures) never appear anywhere in an inventory response,
- the safe description built from the registered tool identity and its
  static registry description is still present and human-readable,
- the authorized detail endpoint keeps serving the exact arguments.

The store-level creation API still accepts a caller-supplied
description (the schema-bound exact request), which is why the only
safe path is the production builder asserted here.
"""

import dataclasses
import hashlib
import json
from typing import Any, ClassVar

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict

from app.core.config import settings
from app.main import app
from app.schemas.approval import ApprovalRequest, ApprovalStatus
from app.schemas.policy import RiskLevel
from app.schemas.tool import ToolResult
from app.services.approval_service import approval_service
from app.services.approval_store import ApprovalStore
from app.services.execution_db import (
    ExecutionStateDb,
)
from app.services.plan_service import plan_service
from app.services.tool_base import Tool
from tests._exec_helpers import OWNER_ID as GRAPH_OWNER
from tests._exec_helpers import SESSION_ID as GRAPH_SESSION, make_graph

DESC_PATH_SESSION = "approvals-desc-path-session-01234567890"
DESC_TOKEN_SESSION = "approvals-desc-token-session-01234567890"

SECRET_PATH_MARKER = "vault/SECRET_ARGUMENT_MARKER_92741"
SECRET_TOKEN_MARKER = "Bearer zk9-TOKEN-shapeD-marker-41700"
NESTED_SECRET = "nested-credential-secret-7f31c2ab92741"

CREATE_DIRECTORY_DESCRIPTION = (
    "create_directory: Create a directory beneath the configured "
    "approved workspace root."
)


def _token_owner() -> str:
    """The owner identity every HTTP call authenticates as."""
    return hashlib.sha256(
        settings.api_token.get_secret_value().encode("utf-8")
    ).hexdigest()


def _headers_for(session_id: str, api_headers: dict) -> dict:
    """Same authenticated identity, different session header."""
    return {
        "Authorization": api_headers["Authorization"],
        "X-JARVIS-Session": session_id,
    }


def _retire(approval_id: str) -> None:
    """Mark one self-created test row purged (metadata-only teardown)."""
    plan_service.execution_db._conn.execute(
        "UPDATE approval_requests SET purged = 1 WHERE approval_id = ?",
        (approval_id,),
    )


def _summary_text(view) -> str:
    """Full text of one summary view, for marker leak assertions."""
    return json.dumps(
        dataclasses.asdict(view), default=str, ensure_ascii=False
    )


def _production_request(session: str, marker: str) -> ApprovalRequest:
    """Create through the REAL singleton service: the applied path."""
    result = approval_service.request_tool(
        "create_directory",
        {"path": marker},
        owner_id=_token_owner(),
        session_id=session,
    )
    assert isinstance(result, ApprovalRequest)
    return result


@pytest.mark.asyncio
async def test_production_description_excludes_flat_argument_values(
    api_headers,
):
    """The full production path keeps the flat marker out of inventory."""
    request = _production_request(DESC_PATH_SESSION, SECRET_PATH_MARKER)
    assert request.arguments == {"path": SECRET_PATH_MARKER}  # bound exactly
    assert request.action_description == CREATE_DIRECTORY_DESCRIPTION

    db = plan_service.execution_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1",
            headers=_headers_for(DESC_PATH_SESSION, api_headers),
        ) as client:
            listing = await client.get("/api/approvals?limit=200")
            detail = await client.get(f"/api/approvals/{request.approval_id}")
    finally:
        _retire(request.approval_id)

    assert listing.status_code == 200
    data = listing.json()
    ours = [
        summary for summary in data["approvals"]
        if summary["approval_id"] == request.approval_id
    ]
    assert len(ours) == 1
    summary = ours[0]
    assert summary["status"] == "PENDING"
    # The description is useful: registered tool identity + static text.
    assert summary["action_description"] == CREATE_DIRECTORY_DESCRIPTION

    # The sensitive argument value is nowhere in the inventory response.
    body = listing.text
    assert SECRET_PATH_MARKER not in body
    assert "SECRET_ARGUMENT_MARKER_92741" not in body
    assert "vault" not in body
    # Raw arguments stay structurally absent (metadata-only summary).
    assert '"arguments"' not in body
    assert '"digest"' not in body

    # The authorized detail endpoint still serves the exact arguments.
    assert detail.status_code == 200
    detail_data = detail.json()
    assert detail_data["status"] == "PENDING"
    assert detail_data["arguments"] == {"path": SECRET_PATH_MARKER}
    assert detail_data["action_description"] == CREATE_DIRECTORY_DESCRIPTION


@pytest.mark.asyncio
async def test_production_description_excludes_token_shaped_values(
    api_headers,
):
    """Token-shaped sensitive arguments never reach the inventory."""
    request = _production_request(DESC_TOKEN_SESSION, SECRET_TOKEN_MARKER)
    assert request.arguments == {"path": SECRET_TOKEN_MARKER}
    assert request.action_description == CREATE_DIRECTORY_DESCRIPTION

    db = plan_service.execution_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1",
            headers=_headers_for(DESC_TOKEN_SESSION, api_headers),
        ) as client:
            listing = await client.get("/api/approvals?limit=200")
            detail = await client.get(
                f"/api/approvals/{request.approval_id}"
            )
    finally:
        _retire(request.approval_id)

    assert listing.status_code == 200
    body = listing.text
    assert SECRET_TOKEN_MARKER not in body
    assert "Bearer" not in body
    assert "zk9-TOKEN-shapeD-marker-41700" not in body
    assert '"arguments"' not in body
    match = [
        summary for summary in listing.json()["approvals"]
        if summary["approval_id"] == request.approval_id
    ]
    assert len(match) == 1
    assert match[0]["action_description"] == CREATE_DIRECTORY_DESCRIPTION

    assert detail.status_code == 200
    assert detail.json()["arguments"] == {"path": SECRET_TOKEN_MARKER}


class _NestedArgumentInput(BaseModel):
    """Nested-structure argument contract for the isolated-path probe."""

    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any]


class NestedArgumentTool(Tool[_NestedArgumentInput]):
    """Approval-required tool accepting nested argument structures."""

    name: ClassVar[str] = "stub_nested"
    description: ClassVar[str] = (
        "Stub tool that accepts nested argument structures."
    )
    read_only: ClassVar[bool] = False
    requires_approval: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW_RISK
    input_model: ClassVar[type[_NestedArgumentInput]] = _NestedArgumentInput

    def execute(self, arguments: _NestedArgumentInput) -> ToolResult:
        return ToolResult.success_result(
            tool_name=self.name,
            message="Nested stub applied its effect.",
        )


def test_nested_argument_structures_never_reach_summaries():
    """Service path with nested arguments: summary stays marker-free.

    Uses the isolated object graph (real ``ApprovalService`` +
    ``ApprovalStore`` over its own façade) because the production
    catalog holds no nested-input tool. The production builder is
    exercised exactly the same way: ``request_tool`` →
    ``_action_description`` → store → ``list_approvals_page``.
    """
    graph = make_graph(with_hanging=False)
    graph.registry.register(NestedArgumentTool())

    request = graph.approvals.request_tool(
        "stub_nested",
        {
            "payload": {
                "credentials": {"token": NESTED_SECRET},
                "nested": {"deep": {"password": NESTED_SECRET}},
            }
        },
        owner_id=GRAPH_OWNER,
        session_id=GRAPH_SESSION,
    )
    assert request.status is ApprovalStatus.PENDING
    safe_description = f"stub_nested: {NestedArgumentTool.description}"
    assert request.action_description == safe_description

    views, total = graph.store.list_approvals_page(
        GRAPH_OWNER, GRAPH_SESSION, limit=50
    )
    assert total == 1
    view = views[0]
    # The summary carries no arguments key and no secret anywhere.
    summary = _summary_text(view)
    assert NESTED_SECRET not in summary
    assert '"arguments"' not in summary
    assert "credentials" not in summary
    assert "password" not in summary
    assert "digest" not in summary
    assert view.action_description == safe_description

    # The stringified nested structure is bound for the detail path.
    detail = graph.store.get_approval_request(
        request.approval_id, GRAPH_OWNER, GRAPH_SESSION
    )
    assert NESTED_SECRET in detail.arguments["payload"]
    assert "credentials" in detail.arguments["payload"]


def test_store_level_requested_description_bound_but_not_inventoried():
    """The store's exact-request contract and the safe builder coexist.

    The store keeps a caller-supplied description verbatim on the
    persisted request (documented compatibility), which is exactly why
    FIX B fixes the production builder instead of only the field: the
    listing serves what was stored, while the arguments themselves stay
    structurally excluded and the authorized detail endpoint keeps
    serving the exact bound arguments.
    """
    store = ApprovalStore(state_db=ExecutionStateDb())
    created = store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": "bound-value-kept-for-detail"},
        action_description="run job (value=argument-rendered-legacy-shape)",
        risk_level="LOW_RISK",
        owner_id=GRAPH_OWNER,
        session_id=GRAPH_SESSION,
    )
    views, total = store.list_approvals_page(
        GRAPH_OWNER, GRAPH_SESSION, limit=50
    )
    assert total == 1
    # The listing serves the stored description verbatim...
    assert views[0].action_description == (
        "run job (value=argument-rendered-legacy-shape)"
    )
    # ...while bound arguments stay structurally excluded from it.
    summary = _summary_text(views[0])
    assert "bound-value-kept-for-detail" not in summary
    assert '"arguments"' not in summary
    # The detail endpoint keeps the exact bound arguments.
    detail = store.get_approval_request(
        created.approval_id, GRAPH_OWNER, GRAPH_SESSION
    )
    assert detail.arguments == {"value": "bound-value-kept-for-detail"}
