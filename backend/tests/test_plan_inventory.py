"""Plan inventory (Phase 5.2-a): read-only listing with identity scoping,
deterministic ordering, one-transaction page+total pairing, advisory
attention flags derived from persisted steps, and strict fail-closed
handling of corrupted plan payloads.

Object graphs for store-level tests are isolated (make_graph over an
in-memory façade, matching the Phase 4.3 test pattern). API tests run
over the shared state DB the whole suite uses, with dedicated throwaway
session identities for deterministic totals and for corruption damage —
a corrupt plan row is session-local and never poisons other identities.
"""

import hashlib
import threading

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.main import app
from app.schemas.tool import ToolResult
from app.services.plan_service import (
    PlanCorruptError,
    plan_service,
    summarize_plan_steps,
)
from tests._exec_helpers import (
    OWNER_ID as GRAPH_OWNER,
    SESSION_ID as GRAPH_SESSION,
    make_graph,
)


EMPTY_PLANS_SESSION = "plans-empty-session-01234567890123"
OTHER_PLANS_SESSION = "plans-other-session-0123456789012"
CORRUPT_PLANS_SESSION = "plans-corrupt-session-012345678901"
ATTENTION_HTTP_SESSION = "plans-attention-session-012345678123"
MAIN_SESSION = "test-session-012345678901234567"

GOAL_TEXT = "inventory-goal-"  # per-test suffix keeps goals distinctive


def _owner_for_session(session_id: str) -> str:
    """The token owner identity every HTTP call authenticates as."""
    return hashlib.sha256(
        settings.api_token.get_secret_value().encode("utf-8")
    ).hexdigest()


def _identity(session_id: str) -> tuple[str, str]:
    return _owner_for_session(session_id), session_id


def _headers_for(session_id: str, api_headers: dict) -> dict:
    """Same authenticated identity, different session header."""
    return {
        "Authorization": api_headers["Authorization"],
        "X-JARVIS-Session": session_id,
    }


def _stub_step(step_id: str = "s1") -> dict:
    return {
        "step_id": step_id,
        "description": f"{step_id} description",
        "tool_name": "stub_ro",
        "arguments": {"value": "x"},
    }


def _make_graph_plan(
    goal: str,
    *,
    owner_id: str = GRAPH_OWNER,
    session_id: str = GRAPH_SESSION,
    tool_name: str = "stub_ro",
    steps: int = 1,
):
    """Create a plan through the graph store (stub tool registered)."""
    return make_graph().plans.create_plan(
        goal=goal,
        steps=[_stub_step(f"s{i}") for i in range(1, steps + 1)],
        owner_id=owner_id,
        session_id=session_id,
    )


def _make_direct_plan(
    goal: str,
    *,
    owner_id: str,
    session_id: str,
    step_ids: tuple[str, ...] = ("s1",),
):
    """Create + persist a plan through the shared (real) plan service."""
    plan = plan_service.create_plan(
        goal=goal,
        steps=[
            {
                "step_id": step_id,
                "description": f"{step_id} description",
                "tool_name": "get_system_info",
            }
            for step_id in step_ids
        ],
        owner_id=owner_id,
        session_id=session_id,
    )
    plan_service.execution_db.save_plan(plan, owner_id, session_id)
    return plan


# --- store level (isolated object graphs) --------------------------------------


def test_plan_page_pairs_page_with_total():
    graph = make_graph()
    made = [
        graph.plans.create_plan(
            goal=f"{GOAL_TEXT}{i}",
            steps=[_stub_step()],
            owner_id=GRAPH_OWNER,
            session_id=GRAPH_SESSION,
        )
        for i in range(5)
    ]
    views, total = graph.plans.list_plans_page(
        owner_id=GRAPH_OWNER, session_id=GRAPH_SESSION, limit=2, offset=1
    )
    # created_at DESC ordering: newest first; offset 1 skips the newest.
    assert [v.plan_id for v in views] == [made[3].plan_id, made[2].plan_id]
    assert total == 5
    # Deterministic: an identical read returns an identical page.
    again, again_total = graph.plans.list_plans_page(
        owner_id=GRAPH_OWNER, session_id=GRAPH_SESSION, limit=2, offset=1
    )
    assert [(v.plan_id, v.created_at, v.updated_at) for v in again] == [
        (v.plan_id, v.created_at, v.updated_at) for v in views
    ]
    assert again_total == total
    # Foreign identity observes an empty page, not foreign rows.
    assert graph.plans.list_plans_page(
        owner_id="foreign-owner", session_id=GRAPH_SESSION, limit=5
    ) == ([], 0)


def test_plan_page_is_identity_scoped():
    graph = make_graph()
    own = [
        graph.plans.create_plan(
            goal=f"{GOAL_TEXT}own{i}",
            steps=[_stub_step()],
            owner_id=GRAPH_OWNER,
            session_id=GRAPH_SESSION,
        )
        for i in range(2)
    ]
    foreign = graph.plans.create_plan(
        goal=f"{GOAL_TEXT}foreign",
        steps=[_stub_step()],
        owner_id="other-owner",
        session_id="other-session",
    )
    views, total = graph.plans.list_plans_page(
        owner_id=GRAPH_OWNER, session_id=GRAPH_SESSION, limit=10
    )
    assert sorted(v.plan_id for v in views) == sorted(
        p.plan_id for p in own
    )
    assert foreign.plan_id not in {v.plan_id for v in views}
    assert total == 2

    foreign_views, foreign_total = graph.plans.list_plans_page(
        owner_id="other-owner", session_id="other-session", limit=10
    )
    assert [v.plan_id for v in foreign_views] == [foreign.plan_id]
    assert foreign_total == 1
    # Same owner, WRONG session: empty (session isolation).
    assert graph.plans.list_plans_page(
        owner_id=GRAPH_OWNER, session_id="other-session", limit=10
    ) == ([], 0)


def test_listing_order_tiebreak_is_deterministic():
    """Equal created_at falls back to plan_id DESC determinism."""
    graph = make_graph()
    made = [
        graph.plans.create_plan(
            goal=f"{GOAL_TEXT}{i}",
            steps=[_stub_step()],
            owner_id=GRAPH_OWNER,
            session_id=GRAPH_SESSION,
        )
        for i in range(3)
    ]
    graph.db._conn.execute(
        "UPDATE plans SET created_at = '2030-01-01T00:00:00+00:00' "
        "WHERE owner_id = ? AND session_id = ?",
        (GRAPH_OWNER, GRAPH_SESSION),
    )
    views, total = graph.plans.list_plans_page(
        owner_id=GRAPH_OWNER, session_id=GRAPH_SESSION, limit=10
    )
    assert [v.plan_id for v in views] == sorted(
        [p.plan_id for p in made], reverse=True
    )
    assert total == 3
    again, _ = graph.plans.list_plans_page(
        owner_id=GRAPH_OWNER, session_id=GRAPH_SESSION, limit=10
    )
    assert [v.plan_id for v in again] == [v.plan_id for v in views]


def test_out_of_bounds_bounds_arguments_fail_closed():
    graph = make_graph()
    with pytest.raises(ValueError):
        graph.plans.list_plans_page(
            owner_id=GRAPH_OWNER, session_id=GRAPH_SESSION, limit=0
        )
    with pytest.raises(ValueError):
        graph.plans.list_plans_page(
            owner_id=GRAPH_OWNER, session_id=GRAPH_SESSION, limit=5, offset=-1
        )


def test_corrupt_payload_fails_closed_strictly():
    """A damaged payload raises; nothing is skipped, repaired, or leaked."""
    graph = make_graph()
    healthy = graph.plans.create_plan(
        goal="stays healthy",
        steps=[_stub_step()],
        owner_id="healthy-owner",
        session_id="healthy-session",
    )
    damaged = graph.plans.create_plan(
        goal="internal-damage-marker",
        steps=[_stub_step()],
        owner_id=GRAPH_OWNER,
        session_id=GRAPH_SESSION,
    )
    graph.db._conn.execute(
        "UPDATE plans SET payload = '{\"steps\": [not json' WHERE plan_id = ?",
        (damaged.plan_id,),
    )

    with pytest.raises(PlanCorruptError) as excinfo:
        graph.plans.list_plans_page(
            owner_id=GRAPH_OWNER, session_id=GRAPH_SESSION, limit=10
        )
    assert str(excinfo.value) == "Plan state could not be validated."
    assert "internal-damage-marker" not in str(excinfo.value)

    # The damaged row still occupies its capacity slot (COUNT is raw);
    # the healthy identity's listing is completely unaffected.
    views, total = graph.plans.list_plans_page(
        owner_id="healthy-owner", session_id="healthy-session", limit=10
    )
    assert [v.plan_id for v in views] == [healthy.plan_id]
    assert total == 1


def test_listing_is_strictly_read_only():
    """Listing never mutates state: statuses stay put across reads."""
    graph = make_graph()
    plan = graph.plans.create_plan(
        goal="stable plan",
        steps=[_stub_step()],
        owner_id=GRAPH_OWNER,
        session_id=GRAPH_SESSION,
    )
    before = graph.db.get_plan_record(plan.plan_id)
    first, _ = graph.plans.list_plans_page(
        owner_id=GRAPH_OWNER, session_id=GRAPH_SESSION, limit=10
    )
    second, _ = graph.plans.list_plans_page(
        owner_id=GRAPH_OWNER, session_id=GRAPH_SESSION, limit=10
    )
    after = graph.db.get_plan_record(plan.plan_id)

    assert first == second
    assert after.plan.model_dump() == before.plan.model_dump()
    assert after.plan.steps[0].status.value == "PENDING"
    assert after.plan.steps[0].result is None


def test_plan_page_pairs_total_under_concurrent_writes():
    """A writer re-saves and creates plans while snapshots are taken;
    every snapshot's (page, total) pairing stays bound by one instant."""
    graph = make_graph()
    made = [
        graph.plans.create_plan(
            goal=f"{GOAL_TEXT}{i}",
            steps=[_stub_step()],
            owner_id=GRAPH_OWNER,
            session_id=GRAPH_SESSION,
        )
        for i in range(4)
    ]
    done = threading.Event()
    writer_error: list[Exception] = []

    def writer():
        try:
            # Bounded creation: three additional plans, then upsert churn.
            for i in range(3):
                graph.plans.create_plan(
                    goal=f"{GOAL_TEXT}late{i}",
                    steps=[_stub_step()],
                    owner_id=GRAPH_OWNER,
                    session_id=GRAPH_SESSION,
                )
            while not done.is_set():
                graph.db.save_plan(made[0], GRAPH_OWNER, GRAPH_SESSION)
        except Exception as exc:  # pragma: no cover - surfaced below
            writer_error.append(exc)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    try:
        seen_totals = 0
        for _ in range(25):
            page, total = graph.plans.list_plans_page(
                owner_id=GRAPH_OWNER, session_id=GRAPH_SESSION, limit=3
            )
            assert len(page) <= 3
            assert total >= len(page)  # page and total from one instant
            stamps = [(v.created_at, v.plan_id) for v in page]
            assert stamps == sorted(stamps, reverse=True)
            seen_totals = max(seen_totals, total)
        assert seen_totals >= 4  # creations became visible at some point
    finally:
        done.set()
        writer_thread.join()
    assert writer_error == []

    # After the writer stopped, page and total are exactly consistent.
    page, total = graph.plans.list_plans_page(
        owner_id=GRAPH_OWNER, session_id=GRAPH_SESSION, limit=10
    )
    assert total == 7
    assert len(page) == 7


def test_summarize_flags_never_label_uncertain_work_safe():
    """Mixed-state plan: a completed step never hides recovery work."""
    graph = make_graph()
    plan = graph.plans.create_plan(
        goal="mixed-state",
        steps=[
            _stub_step("ro"),
            {
                "step_id": "awaits",
                "description": "pending approval",
                "tool_name": "stub_mut_ok",
                "arguments": {"value": "x"},
                "depends_on": ["ro"],
            },
            {
                "step_id": "uncertain",
                "description": "uncertain mutation",
                "tool_name": "stub_mut_ok",
                "arguments": {"value": "x"},
                "depends_on": ["ro"],
            },
        ],
        owner_id=GRAPH_OWNER,
        session_id=GRAPH_SESSION,
    )
    graph.plans.record_result(
        plan.plan_id,
        "ro",
        ToolResult.success_result(tool_name="stub_ro", message="done"),
        GRAPH_OWNER,
        GRAPH_SESSION,
    )
    approval = graph.plans.request_step_execution(
        plan.plan_id, "awaits", GRAPH_OWNER, GRAPH_SESSION
    )
    assert approval.approval_id  # mutating tool -> pending approval
    assert graph.plans.request_step_execution(
        plan.plan_id, "uncertain", GRAPH_OWNER, GRAPH_SESSION
    ).approval_id

    # Uncertain step: AWAITING_APPROVAL -> RECOVERY_REQUIRED (human work).
    assert graph.plans.mark_step_uncertain_from_recovery(
        plan.plan_id, "uncertain", GRAPH_OWNER, GRAPH_SESSION
    )

    views, _ = graph.plans.list_plans_page(
        owner_id=GRAPH_OWNER, session_id=GRAPH_SESSION, limit=5
    )
    derived = summarize_plan_steps(views[0].plan)
    assert derived["status_counts"] == {
        "COMPLETED": 1,
        "AWAITING_APPROVAL": 1,
        "RECOVERY_REQUIRED": 1,
    }
    assert derived["recovery_required"] == ["uncertain"]
    assert derived["awaiting_approval"] == ["awaits"]
    assert derived["needs_attention"] is True

    # Pure function: reading again derives the identical flags.
    again, _ = graph.plans.list_plans_page(
        owner_id=GRAPH_OWNER, session_id=GRAPH_SESSION, limit=5
    )
    assert summarize_plan_steps(again[0].plan) == derived


def test_summarize_flags_for_terminal_and_pending_plans():
    """Pure-function derivation for non-attention plan shapes."""
    graph = make_graph()
    plan = graph.plans.create_plan(
        goal="statuses",
        steps=[_stub_step("only")],
        owner_id=GRAPH_OWNER,
        session_id=GRAPH_SESSION,
    )
    assert summarize_plan_steps(plan) == {
        "status_counts": {"PENDING": 1},
        "recovery_required": [],
        "awaiting_approval": [],
        "needs_attention": False,
    }
    graph.plans.record_result(
        plan.plan_id,
        "only",
        ToolResult.failure_result(
            tool_name="stub_ro", error_code="stub", message="failed"
        ),
        GRAPH_OWNER,
        GRAPH_SESSION,
    )
    fetched = graph.plans.get_plan(plan.plan_id, GRAPH_OWNER, GRAPH_SESSION)
    derived = summarize_plan_steps(fetched)
    assert derived["status_counts"] == {"FAILED": 1}
    assert derived["needs_attention"] is False  # terminal, honest failure


# --- API level (shared state DB, HTTP boundary) ---------------------------------


@pytest_asyncio.fixture
async def client(api_headers):
    """Async test client for the authenticated local owner."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        headers=api_headers,
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_list_plans_empty_for_throwaway_session(api_headers):
    """An identity with no plans lists an empty page (default echo)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers=_headers_for(EMPTY_PLANS_SESSION, api_headers),
    ) as fresh:
        response = await fresh.get("/api/plans")
    assert response.status_code == 200
    assert response.json() == {
        "plans": [],
        "total": 0,
        "limit": 50,
        "offset": 0,
    }


@pytest.mark.asyncio
async def test_list_plans_returns_summaries_and_counts(client: AsyncClient):
    """Metadata-only summaries with accurate status derivation."""
    owner, session = _identity(MAIN_SESSION)
    plan_ok = _make_direct_plan(
        GOAL_TEXT + "counts-ok",
        owner_id=owner,
        session_id=session,
        step_ids=("s1",),
    )
    plan_service.record_result(
        plan_ok.plan_id,
        "s1",
        ToolResult.success_result(tool_name="get_system_info", message="ok"),
        owner_id=owner,
        session_id=session,
    )
    plan_pending = _make_direct_plan(
        GOAL_TEXT + "counts-pending",
        owner_id=owner,
        session_id=session,
        step_ids=("s1", "s2"),
    )

    response = await client.get("/api/plans?limit=200")
    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) == {"plans", "total", "limit", "offset"}
    assert data["limit"] == 200 and data["offset"] == 0
    assert data["total"] >= 2

    listed = {p["plan_id"]: p for p in data["plans"]}
    assert listed, "expected a non-empty page"
    ok_view = listed[plan_ok.plan_id]
    assert set(ok_view.keys()) == {
        "plan_id", "goal", "created_at", "updated_at", "step_count",
        "status_counts", "recovery_required", "awaiting_approval",
        "needs_attention",
    }
    assert ok_view["goal"] == GOAL_TEXT + "counts-ok"
    assert ok_view["step_count"] == 1
    assert ok_view["status_counts"] == {"COMPLETED": 1}
    assert ok_view["needs_attention"] is False  # terminal, nothing unresolved
    assert ok_view["recovery_required"] == []
    assert ok_view["awaiting_approval"] == []

    pending_view = listed[plan_pending.plan_id]
    assert pending_view["step_count"] == 2
    assert pending_view["status_counts"] == {"PENDING": 2}
    assert pending_view["needs_attention"] is False  # nothing unresolved

    # Metadata-only: never step arguments, results, or identity fields.
    for summary in data["plans"]:
        assert "steps" not in summary
        assert "arguments" not in summary
        assert "owner_id" not in summary and "session_id" not in summary


@pytest.mark.asyncio
async def test_list_plans_flags_attention_for_approval_and_recovery(
    client: AsyncClient, api_headers
):
    """A plan holding AWAITING_APPROVAL work flags attention (advisory:
    the listing itself reconciles nothing)."""
    owner, session = _identity(ATTENTION_HTTP_SESSION)
    graph = make_graph(state_db=plan_service.execution_db)
    plan = graph.plans.create_plan(
        goal="api-attention-flagged-plan",
        steps=[
            _stub_step("ro"),
            {
                "step_id": "awaits",
                "description": "pending approval",
                "tool_name": "stub_mut_ok",
                "arguments": {"value": "x"},
                "depends_on": ["ro"],
            },
        ],
        owner_id=owner,
        session_id=session,
    )
    graph.plans.record_result(
        plan.plan_id,
        "ro",
        ToolResult.success_result(tool_name="stub_ro", message="done"),
        owner_id=owner,
        session_id=session,
    )
    assert graph.plans.request_step_execution(
        plan.plan_id, "awaits", owner, session
    ).approval_id

    response = await client.get(
        "/api/plans?limit=200",
        headers=_headers_for(ATTENTION_HTTP_SESSION, api_headers),
    )
    assert response.status_code == 200
    data = response.json()
    flagged = [p for p in data["plans"] if p["plan_id"] == plan.plan_id]
    assert len(flagged) == 1
    view = flagged[0]
    assert view["status_counts"] == {
        "COMPLETED": 1,
        "AWAITING_APPROVAL": 1,
    }
    assert view["awaiting_approval"] == ["awaits"]
    assert view["recovery_required"] == []
    assert view["needs_attention"] is True


@pytest.mark.asyncio
async def test_list_plans_pagination_partitions_deterministically(
    client: AsyncClient,
):
    owner, session = _identity(MAIN_SESSION)
    created = [
        _make_direct_plan(
            GOAL_TEXT + f"page{i}",
            owner_id=owner,
            session_id=session,
            step_ids=("s1",),
        )
        for i in range(5)
    ]
    # Force strictly increasing created_at timestamps so newest-first
    # ordering is observable exactly (microsecond ties would fall back
    # to the plan_id tiebreak, which is correct but not index-predictable).
    for index, plan in enumerate(created):
        plan_service.execution_db._conn.execute(
            "UPDATE plans SET created_at = ? WHERE plan_id = ?",
            (f"2030-01-01T00:00:0{index}+00:00", plan.plan_id),
        )
    page_one = (await client.get("/api/plans?limit=3&offset=0")).json()
    page_two = (await client.get("/api/plans?limit=3&offset=3")).json()

    seen = [p["plan_id"] for p in page_one["plans"]] + [
        p["plan_id"] for p in page_two["plans"]
    ]
    assert len(set(seen)) == len(seen)  # disjoint pages
    for plan in created:
        assert plan.plan_id in seen
    # Newest-first ordering across the partition boundary: the forced
    # 2030 timestamps make these five the newest of this shared
    # identity (earlier tests' plan rows have 2026 timestamps and trail
    # after them). Anything beyond our five is pre-existing suite state.
    ids_by_created = [p.plan_id for p in reversed(created)]
    assert seen[:5] == ids_by_created
    assert page_one["total"] >= 5

    repeat = (await client.get("/api/plans?limit=3&offset=0")).json()
    assert [p["plan_id"] for p in repeat["plans"]] == [
        p["plan_id"] for p in page_one["plans"]
    ]


@pytest.mark.asyncio
async def test_list_plans_invalid_pagination_is_422(client: AsyncClient):
    for bad in ("limit=0", "limit=201", "limit=-5", "limit=abc", "offset=-1"):
        response = await client.get(f"/api/plans?{bad}")
        assert response.status_code == 422, bad
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_list_plans_requires_authentication(client: AsyncClient):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as unauthenticated:
        response = await unauthenticated.get("/api/plans")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "HTTP_401"

    async with AsyncClient(
        transport=transport, base_url="http://127.0.0.1",
        headers={
            "Authorization": "Bearer not-the-real-token-value-0123456789",
            "X-JARVIS-Session": "forged-session-012345678901234",
        },
    ) as invalid:
        invalid_response = await invalid.get("/api/plans")
    assert invalid_response.status_code == 401
    assert invalid_response.json()["error"]["code"] == "HTTP_401"


@pytest.mark.asyncio
async def test_list_plans_identity_isolation(
    client: AsyncClient, api_headers
):
    """Each session sees exactly its own plans; foreign rows never leak."""
    owner, own_session = _identity(MAIN_SESSION)
    mine = _make_direct_plan(
        GOAL_TEXT + "isolation-mine",
        owner_id=owner,
        session_id=own_session,
        step_ids=("s1",),
    )
    other_owner, other_session = _identity(OTHER_PLANS_SESSION)
    theirs = _make_direct_plan(
        GOAL_TEXT + "isolation-theirs",
        owner_id=other_owner,
        session_id=other_session,
        step_ids=("s1",),
    )

    my_list = (await client.get("/api/plans?limit=200")).json()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1",
        headers=_headers_for(OTHER_PLANS_SESSION, api_headers),
    ) as other:
        their_list = (await other.get("/api/plans?limit=200")).json()

    my_ids = {p["plan_id"] for p in my_list["plans"]}
    assert mine.plan_id in my_ids
    assert theirs.plan_id not in my_ids
    their_ids = {p["plan_id"] for p in their_list["plans"]}
    assert theirs.plan_id in their_ids
    assert mine.plan_id not in their_ids


@pytest.mark.asyncio
async def test_list_plans_corrupt_payload_maps_to_structured_500(
    client: AsyncClient, api_headers
):
    """Real corruption end to end: a damaged plan fails closed (500)."""
    owner, session = _identity(CORRUPT_PLANS_SESSION)
    plan = _make_direct_plan(
        GOAL_TEXT + "corrupt-marker",
        owner_id=owner,
        session_id=session,
        step_ids=("s1",),
    )
    plan_service.execution_db._conn.execute(
        "UPDATE plans SET payload = '{\"goal\": 42, \"steps\": [nope' "
        "WHERE plan_id = ?",
        (plan.plan_id,),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1",
        headers=_headers_for(CORRUPT_PLANS_SESSION, api_headers),
    ) as damaged:
        response = await damaged.get("/api/plans")
    assert response.status_code == 500
    data = response.json()
    assert data["error"]["code"] == "HTTP_500"
    assert data["error"]["message"] == "Plan state could not be validated."
    # Corrupted internals never leak into the response.
    assert "corrupt-marker" not in response.text
    assert "nope" not in response.text

    # The damaged row is session-local: other identities stay served.
    healthy = await client.get("/api/plans?limit=200")
    assert healthy.status_code == 200


@pytest.mark.asyncio
async def test_list_plans_is_read_only_over_http(client: AsyncClient):
    """Listing twice returns identical bytes and changes nothing."""
    owner, session = _identity(MAIN_SESSION)
    plan = _make_direct_plan(
        GOAL_TEXT + "readonly",
        owner_id=owner,
        session_id=session,
        step_ids=("s1",),
    )

    before = await client.get(f"/api/plans/{plan.plan_id}")
    assert before.status_code == 200
    before_json = before.json()

    first = await client.get("/api/plans?limit=200")
    assert first.status_code == 200
    second = await client.get("/api/plans?limit=200")
    assert second.status_code == 200
    assert first.content == second.content

    after = await client.get(f"/api/plans/{plan.plan_id}")
    assert after.status_code == 200
    assert after.json() == before_json
    assert all(step["status"] == "PENDING" for step in after.json()["steps"])