"""Detail-path corruption handling (Phase 5.2-c, revisited in 5.3).

``GET /api/plans/{plan_id}`` and ``GET /api/approvals/{approval_id}``
fail closed for damaged persisted state with a structured 500 and a
fixed, non-sensitive message. Authorization runs BEFORE any payload
parse: unknown and foreign resources observe 404 — never the row's
state — even when that row is corrupt.

Plan-side damage lives in the shared database in a throwaway session
(no global scan parses plan payloads, so the row cannot poison other
suites). Approval-side damage lives in an ISOLATED object graph
monkeypatched onto the route: since Phase 5.3 the approval cleanup
scan is row-local and never parses sibling payloads, but fail-closed
discipline still keeps self-created damage session-local — the owning
identity's reads fail closed and the shared database is never left
with damaged payloads for other suites to read.
"""

import hashlib

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.main import app
from app.schemas.approval import ApprovalStatus
from app.services.approval_service import ApprovalService
from app.services.approval_store import (
    ApprovalCorruptError,
    ApprovalNotFoundError,
)
from app.services.plan_service import (
    PlanCorruptError,
    PlanNotFoundError,
    plan_service,
)
from tests._exec_helpers import OWNER_ID as GRAPH_OWNER
from tests._exec_helpers import SESSION_ID as GRAPH_SESSION
from tests._exec_helpers import make_graph
from tests.conftest import OTHER_SESSION_ID as API_OTHER_SESSION
from tests.conftest import TEST_SESSION_ID as API_SESSION

PLAN_CORRUPT_SESSION = "plans-detail-corrupt-session-012345678"
PLAN_HEALTHY_SESSION = "plans-detail-healthy-session-012345678"
PLAN_OTHER_SESSION = "plans-detail-other-session-0123456789"
CORRUPT_PAYLOAD = '{"plan_id": "x", "goal": 42, "steps": [corrupt-marker-absent'
APP_CORRUPT_PAYLOAD = '{"tool_name": "corrupt-marker-absent", "args": [nope'
CORRUPT_MARKER = "corrupt-marker-absent"

FIXED_PLAN_MESSAGE = "Plan state could not be validated."
FIXED_APPROVAL_MESSAGE = "Approval state could not be validated."

APPROVAL_ARG_MARKER = "detail-pass-argument-marker-do-not-leak"


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


def _direct_plan(goal: str, owner_id: str, session_id: str):
    """Create + persist a plan through the shared (real) plan service."""
    return plan_service.create_plan(
        goal=goal,
        steps=[
            {
                "step_id": "s1",
                "description": "detail probe step",
                "tool_name": "get_system_info",
                "arguments": {},
            }
        ],
        owner_id=owner_id,
        session_id=session_id,
    )


def _invalidated_plan(plan_id: str) -> None:
    """Persistently damage one plan row in the shared database."""
    plan_service.execution_db._conn.execute(
        f"UPDATE plans SET payload = '{CORRUPT_PAYLOAD}' WHERE plan_id = ?",
        (plan_id,),
    )


def _damaged_approval(graphdb, approval_id: str) -> None:
    """Persistently damage one approval row in the ISOLATED database."""
    graphdb._conn.execute(
        f"UPDATE approval_requests SET payload = '{APP_CORRUPT_PAYLOAD}' "
        "WHERE approval_id = ?",
        (approval_id,),
    )


# --- plan detail (shared database, throwaway sessions) -------------------------


@pytest.mark.asyncio
async def test_plan_detail_corrupt_maps_to_structured_500(api_headers):
    owner = _token_owner()
    plan = _direct_plan("detail-corrupt-plan", owner, PLAN_CORRUPT_SESSION)
    _invalidated_plan(plan.plan_id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1",
        headers=_headers_for(PLAN_CORRUPT_SESSION, api_headers),
    ) as damaged:
        response = await damaged.get(f"/api/plans/{plan.plan_id}")

    assert response.status_code == 500
    data = response.json()
    assert data["error"]["code"] == "HTTP_500"
    assert data["error"]["message"] == FIXED_PLAN_MESSAGE
    assert data["error"]["request_id"]
    # No payload/args/digest/internals ever leak into the response.
    assert CORRUPT_MARKER not in response.text
    assert "digest" not in response.text
    assert "owner_id" not in response.text
    assert "session_id" not in response.text


@pytest.mark.asyncio
async def test_plan_detail_corrupt_foreign_and_unknown_observe_404(api_headers):
    owner = _token_owner()
    plan = _direct_plan("detail-corrupt-plan-404", owner, PLAN_CORRUPT_SESSION)
    _invalidated_plan(plan.plan_id)

    # Foreign session: the corrupt row must never reveal itself.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1",
        headers=_headers_for(PLAN_OTHER_SESSION, api_headers),
    ) as foreign:
        foreign_read = await foreign.get(f"/api/plans/{plan.plan_id}")
    assert foreign_read.status_code == 404
    assert foreign_read.json()["error"]["message"] == "Plan was not found"
    assert CORRUPT_MARKER not in foreign_read.text

    # Unknown plan id stays 404.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1",
        headers=api_headers,
    ) as client:
        unknown = await client.get("/api/plans/does-not-exist-123456")
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_plan_detail_still_serves_healthy_rows(api_headers):
    """Positive control: mapping corruption breaks nothing for valid state."""
    owner = _token_owner()
    plan = _direct_plan("detail-healthy-plan", owner, PLAN_HEALTHY_SESSION)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1",
        headers=_headers_for(PLAN_HEALTHY_SESSION, api_headers),
    ) as client:
        response = await client.get(f"/api/plans/{plan.plan_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["plan_id"] == plan.plan_id
    assert data["overall_status"] == "actionable"


# --- approval detail (isolated graph monkeypatched onto the route) -------------


@pytest.fixture
def isolated_approvals(monkeypatch):
    """Route the approval API onto an isolated object graph."""
    graph = make_graph()
    monkeypatch.setattr("app.api.approvals.approval_service", graph.approvals)
    return graph


def _isolated_create(graph, session: str = API_SESSION, owner: str | None = None):
    """Create an approval; defaults to the ROUTE's identity (token owner)."""
    return graph.store.create_approval_request(
        tool_name="stub_mut_ok",
        arguments={"value": APPROVAL_ARG_MARKER},
        action_description="detail corruption probe",
        risk_level="LOW_RISK",
        owner_id=owner if owner is not None else _token_owner(),
        session_id=session,
    )


@pytest.mark.asyncio
async def test_approval_detail_corrupt_maps_to_structured_500(
    isolated_approvals, api_headers
):
    graph = isolated_approvals
    healthy = _isolated_create(graph)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1",
        headers=api_headers,
    ) as client:
        # Positive control first: healthy rows serve fully, arguments included.
        ok = await client.get(f"/api/approvals/{healthy.approval_id}")
        assert ok.status_code == 200
        assert ok.json()["arguments"] == {"value": APPROVAL_ARG_MARKER}

        # Damage a row, then read THAT row: fixed, non-sensitive 500.
        damaged = _isolated_create(graph)
        _damaged_approval(graph.db, damaged.approval_id)
        response = await client.get(f"/api/approvals/{damaged.approval_id}")

    assert response.status_code == 500
    data = response.json()
    assert data["error"]["code"] == "HTTP_500"
    assert data["error"]["message"] == FIXED_APPROVAL_MESSAGE
    assert data["error"]["request_id"]
    assert CORRUPT_MARKER not in response.text
    assert "nope" not in response.text
    assert "digest" not in response.text
    assert "owner_id" not in response.text


@pytest.mark.asyncio
async def test_approval_detail_unknown_stays_404(isolated_approvals, api_headers):
    """Clean store: unknown approval ids observe not-found as before."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1",
        headers=api_headers,
    ) as client:
        unknown = await client.get("/api/approvals/does-not-exist-987654321")
    assert unknown.status_code == 404
    assert "Approval request was not found" in unknown.text


@pytest.mark.asyncio
async def test_approval_detail_corrupt_foreign_row_stays_404(
    isolated_approvals, api_headers
):
    """Phase 5.3: a foreign corrupt row is answered like any foreign row.

    The identity gate runs before any payload parse and the row-local
    maintenance scan no longer parses sibling payloads, so the corrupt
    foreign row cannot reveal itself: 404 with the fixed not-found
    message and no payload content.
    """
    graph = isolated_approvals
    damaged = _isolated_create(graph, session=API_OTHER_SESSION)
    _damaged_approval(graph.db, damaged.approval_id)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1",
        headers=api_headers,
    ) as client:
        foreign_read = await client.get(f"/api/approvals/{damaged.approval_id}")

    assert foreign_read.status_code == 404
    assert "Approval request was not found" in foreign_read.text
    assert CORRUPT_MARKER not in foreign_read.text
    assert "owner_id" not in foreign_read.text


# --- service / store level pinning --------------------------------------------


def test_owned_corrupt_record_raises_dedicated_error():
    """Store level: owned + corrupt -> ApprovalCorruptError, fixed message."""
    graph = make_graph()
    damaged = _isolated_create(graph, session=GRAPH_SESSION, owner=GRAPH_OWNER)
    _damaged_approval(graph.db, damaged.approval_id)

    with pytest.raises(ApprovalCorruptError) as excinfo:
        graph.store._owned_record(damaged.approval_id, GRAPH_OWNER, GRAPH_SESSION)
    assert str(excinfo.value) == FIXED_APPROVAL_MESSAGE


def test_owned_corrupt_record_foreign_stays_not_found():
    """Store level: the identity/purge gate runs before any payload parse.

    Same owner, different session, corrupt payload: parse-first would
    raise the corrupt error; the ordered gate answers not-found.
    """
    graph = make_graph()
    damaged = _isolated_create(graph, session=API_OTHER_SESSION)
    _damaged_approval(graph.db, damaged.approval_id)

    with pytest.raises(ApprovalNotFoundError):
        graph.store._owned_record(damaged.approval_id, _token_owner(), API_SESSION)


def test_service_get_request_translates_corruption():
    """Service level: owned corrupt rows surface the dedicated exception.

    Phase 5.3: the identity here must match the row's own owner/session
    — the scan no longer raises for sibling rows, so the owned gate is
    what admits the corrupted read to the typed-parse translation.
    """
    graph = make_graph()
    damaged = _isolated_create(
        graph, session=GRAPH_SESSION, owner=GRAPH_OWNER
    )
    _damaged_approval(graph.db, damaged.approval_id)

    with pytest.raises(ApprovalCorruptError) as excinfo:
        graph.approvals.get_request(damaged.approval_id, GRAPH_OWNER, GRAPH_SESSION)
    assert str(excinfo.value) == FIXED_APPROVAL_MESSAGE


def test_service_get_request_translates_raw_parse_failures():
    """Service level: raw TypeError/ValueError becomes the typed error.

    This covers state reachable through the maintenance scan today
    (see the deferred-debt note in the phase report).
    """
    graph = make_graph()
    healthy = _isolated_create(graph)

    def raw_failure(*args, **kwargs):
        raise ValueError(f"raw junk parse {CORRUPT_MARKER}")

    graph.approvals._store.get_approval_request = raw_failure
    with pytest.raises(ApprovalCorruptError) as excinfo:
        graph.approvals.get_request(healthy.approval_id, GRAPH_OWNER, GRAPH_SESSION)
    assert str(excinfo.value) == FIXED_APPROVAL_MESSAGE
    assert CORRUPT_MARKER not in str(excinfo.value)


def test_service_get_request_passes_typed_corruption_through():
    """Service level: ApprovalCorruptError is never re-wrapped."""
    graph = make_graph()
    healthy = _isolated_create(graph)
    typed = ApprovalCorruptError(FIXED_APPROVAL_MESSAGE)

    def typed_failure(*args, **kwargs):
        raise typed

    graph.approvals._store.get_approval_request = typed_failure
    with pytest.raises(ApprovalCorruptError) as excinfo:
        graph.approvals.get_request(healthy.approval_id, GRAPH_OWNER, GRAPH_SESSION)
    assert excinfo.value is typed


def test_corrupt_sibling_row_no_longer_poisons_healthy_owned_read():
    """Phase 5.3: the row-local scan never parses sibling payloads.

    A corrupt sibling row in the same store no longer breaks an
    otherwise healthy owned read: the maintenance scan drives its
    expiry/retention decisions from persisted metadata columns and
    handles each row locally. The damaged row's own identity keeps
    fail closing (pinned above); unrelated reads stay served.
    """
    graph = make_graph()
    healthy = _isolated_create(
        graph, session=GRAPH_SESSION, owner=GRAPH_OWNER
    )
    damaged = _isolated_create(
        graph, session=GRAPH_SESSION, owner=GRAPH_OWNER
    )
    _damaged_approval(graph.db, damaged.approval_id)

    request = graph.approvals.get_request(
        healthy.approval_id, GRAPH_OWNER, GRAPH_SESSION
    )
    assert request.approval_id == healthy.approval_id
    assert request.status is ApprovalStatus.PENDING
    assert request.arguments == {"value": APPROVAL_ARG_MARKER}


# --- plan service level pinning ------------------------------------------------


def test_plan_owned_corrupt_raises_dedicated_error():
    """Service level: owned + corrupt -> PlanCorruptError, fixed message."""
    owner = _token_owner()
    plan = _direct_plan("gate-pin-plan-corrupt", owner, PLAN_CORRUPT_SESSION)
    _invalidated_plan(plan.plan_id)
    with pytest.raises(PlanCorruptError) as excinfo:
        plan_service._owned_plan(plan.plan_id, owner, PLAN_CORRUPT_SESSION)
    assert str(excinfo.value) == FIXED_PLAN_MESSAGE


def test_plan_owned_corrupt_foreign_stays_not_found():
    """Service level: the identity gate runs before any payload parse.

    Same owner, different session, corrupt payload: parse-first would
    raise the corrupt error; the ordered gate answers not-found.
    """
    owner = _token_owner()
    plan = _direct_plan("gate-pin-plan-foreign", owner, PLAN_CORRUPT_SESSION)
    _invalidated_plan(plan.plan_id)
    with pytest.raises(PlanNotFoundError):
        plan_service._owned_plan(plan.plan_id, owner, API_SESSION)
