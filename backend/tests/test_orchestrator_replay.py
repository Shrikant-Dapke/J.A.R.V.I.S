"""Bounded orchestrator replay (Phase 5.1-c): the newest complete turns
within the configured budgets reach the provider; persisted history is
never truncated, and identity boundaries are unaffected.

The provider call may see at most MAX_HISTORY_REPLAY_TURNS turns and
MAX_HISTORY_REPLAY_CHARS characters of history, followed by the
orchestrator instruction message. Selection is the newest contiguous
span of whole turns (a sliding window) - never a mid-history gap, never
a truncated message. Scripted providers record every message list they
receive, so each test compares the recorded window directly.
"""

from datetime import datetime
from uuid import uuid4

import pytest

from app.schemas.conversation import ConversationTurn
from app.services.approval_service import ApprovalService
from app.services.approval_store import ApprovalStore
from app.services.conversation_store import (
    ConversationNotFoundError,
    PersistentConversationStore,
)
from app.services.orchestrator import (
    MAX_HISTORY_REPLAY_CHARS,
    MAX_HISTORY_REPLAY_TURNS,
    ORCHESTRATOR_INSTRUCTION_PREFIX,
    ChatOrchestrator,
    select_replay_window,
)
from app.services.plan_service import PlanService
from app.services.tool_executor import ToolExecutor
from app.services.tool_registry import tool_registry


OWNER_ID = "replay-owner"
SESSION_ID = "replay-session"

DECISION_REPLY = '{"action": "reply", "reply": "Direct reply."}'
DECISION_SYSTEM_INFO = (
    '{"action": "tool_action", "tool": "get_system_info", '
    '"arguments": {}, "explanation": "Reading system info."}'
)


class ScriptedProvider:
    """Provider returning queued responses and recording each message list."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def generate_response(self, messages):
        from app.schemas.llm import LLMResponse

        self.calls.append(list(messages))
        if len(self.calls) > len(self._responses):
            raise AssertionError("unexpected extra provider call")
        return LLMResponse(
            content=self._responses[len(self.calls) - 1], model="scripted"
        )


def fresh_orchestrator():
    """Isolated store + scripted reply-provider orchestrator."""
    store = PersistentConversationStore()
    provider = ScriptedProvider([DECISION_REPLY, DECISION_REPLY])
    approvals = ApprovalService(
        registry=tool_registry,
        executor=ToolExecutor(tool_registry),
        store=ApprovalStore(),
    )
    orchestrator = ChatOrchestrator(
        provider_factory=lambda: provider,
        approvals=approvals,
        plans=PlanService(registry=tool_registry, approvals=approvals),
        registry=tool_registry,
        conversations=store,
    )
    return orchestrator, store, provider


def fill_history(store, conv_id, count, content="o" * 1000, role="user"):
    for _ in range(count):
        store.add_turn(
            conv_id, role, content, owner_id=OWNER_ID, session_id=SESSION_ID
        )


def _turn(role, content, stamp="2030-01-01T00:00:00+00:00"):
    return ConversationTurn(
        role=role, content=content, timestamp=datetime.fromisoformat(stamp)
    )


# --- selector unit tests (pure function, no store involved) --------------------


def test_empty_history_selects_nothing():
    assert select_replay_window([]) == []


def test_history_below_both_limits_is_fully_replayed():
    history = [
        _turn("user", "q1"), _turn("assistant", "a1"),
        _turn("user", "q2"), _turn("assistant", "a2"),
    ]
    window = select_replay_window(history)
    assert [t.content for t in window] == ["q1", "a1", "q2", "a2"]
    assert [t.role for t in window] == ["user", "assistant", "user", "assistant"]


def test_exactly_limit_turns_are_all_replayed():
    history = [_turn("user", f"t{i}") for i in range(MAX_HISTORY_REPLAY_TURNS)]
    window = select_replay_window(history)
    assert len(window) == MAX_HISTORY_REPLAY_TURNS == 24
    assert [t.content for t in window] == [f"t{i}" for i in range(24)]


def test_more_than_limit_turns_keeps_newest_contiguous_span():
    total = MAX_HISTORY_REPLAY_TURNS + 6
    history = [_turn("user", f"t{i}") for i in range(total)]
    window = select_replay_window(history)
    assert len(window) == MAX_HISTORY_REPLAY_TURNS
    # The OLDEST 6 turns are gone; the window is a contiguous recent span.
    assert [t.content for t in window] == [f"t{i}" for i in range(6, total)]
    assert window[0].content == "t6"
    assert window[-1].content == f"t{total - 1}"


def test_exactly_char_budget_fits():
    """Budget equality is not overflow: exactly 12,000 chars replay."""
    # 10 older turns of 1,100 chars + current 1,000 chars == 12,000 exactly.
    history = [_turn("user", "x" * 1100) for _ in range(10)]
    history.append(_turn("user", "y" * 1000))
    assert sum(len(t.content) for t in history) == MAX_HISTORY_REPLAY_CHARS
    window = select_replay_window(history)
    assert sum(len(t.content) for t in window) == MAX_HISTORY_REPLAY_CHARS
    assert len(window) == 11


def test_character_overflow_drops_oldest_whole_turns():
    # Newest-first: current 100 chars, older turns 1,000 each. Turns fit
    # while used + 1000 <= 12,000 -> 11 older turns fit (11,100 total);
    # the 12th would overflow and the backward walk stops there.
    history = [_turn("user", "o" * 1000) for _ in range(30)]
    history.append(_turn("user", "c" * 100))
    window = select_replay_window(history)
    assert len(window) == 12
    assert sum(len(t.content) for t in window) == 11 * 1000 + 100
    assert window[0].content == "o" * 1000
    assert window[-1].content == "c" * 100
    # Nothing was truncated to squeeze content in.
    assert all(len(t.content) == 1000 for t in window[:-1])


def test_single_oversized_current_message_is_never_dropped():
    """The current user message always reaches the provider."""
    oversized = "z" * (MAX_HISTORY_REPLAY_CHARS + 5_000)
    history = [_turn("assistant", "older stable text"), _turn("user", oversized)]
    window = select_replay_window(history)
    assert [(t.role, t.content) for t in window] == [("user", oversized)]


def test_single_oversized_historical_turn_stops_the_walk():
    """Documented contiguity rule: an unfittable turn halts backward
    selection; no older turn is smuggled around it."""
    oversized = "z" * (MAX_HISTORY_REPLAY_CHARS + 1)
    history = [
        _turn("user", "tiny-old"),        # would still fit alone
        _turn("assistant", oversized),    # does not fit
        _turn("assistant", "small-new"),  # fits alongside current
        _turn("user", "current"),
    ]
    window = select_replay_window(history)
    assert [t.content for t in window] == ["small-new", "current"]


def test_mixed_sizes_fill_newest_first():
    history = [
        _turn("assistant", "a" * 6000),   # excluded: unfittable
        _turn("user", "b" * 4900),        # excluded: would overflow with c+d
        _turn("assistant", "c" * 4900),   # fits: 4,900 + current 4,000
        _turn("user", "d" * 4000),        # current
    ]
    window = select_replay_window(history)
    assert [t.content for t in window] == ["c" * 4900, "d" * 4000]


def test_current_message_survives_turn_budget_exhaustion():
    current = _turn("user", "the current question")
    history = [_turn("assistant", f"a{i}") for i in range(40)] + [current]
    window = select_replay_window(history)
    assert len(window) == MAX_HISTORY_REPLAY_TURNS == 24
    assert window[-1] is current


def test_selection_never_mutates_the_input():
    history = [_turn("user", f"t{i}") for i in range(30)]
    snapshot = [(t.role, t.content, t.timestamp) for t in history]
    window = select_replay_window(history)
    assert len(window) < len(history)
    assert snapshot == [(t.role, t.content, t.timestamp) for t in history]


# --- integration through ChatOrchestrator -------------------------------------


def test_provider_receives_bounded_window_and_instruction():
    total = MAX_HISTORY_REPLAY_TURNS + 10
    orchestrator, store, provider = fresh_orchestrator()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    for i in range(total):
        store.add_turn(
            conv_id, "user" if i % 2 else "assistant", f"t{i}",
            owner_id=OWNER_ID, session_id=SESSION_ID,
        )

    orchestrator.handle_chat_message(
        "the current question", conv_id,
        owner_id=OWNER_ID, session_id=SESSION_ID,
    )

    assert len(provider.calls) == 1
    window, instruction = provider.calls[0][:-1], provider.calls[0][-1]
    # Exactly 24 history turns + the trailing instruction.
    assert len(window) == MAX_HISTORY_REPLAY_TURNS
    assert [(m.role, m.content) for m in window] == [
        ("user" if i % 2 else "assistant", f"t{i}")
        for i in range(total - MAX_HISTORY_REPLAY_TURNS + 1, total)
    ] + [("user", "the current question")]
    # The trusted instruction is preserved as the final message.
    assert instruction.role == "user"
    assert instruction.content.startswith(ORCHESTRATOR_INSTRUCTION_PREFIX)
    assert "the current question" in instruction.content


def test_small_history_is_replayed_unchanged_in_full():
    orchestrator, _, provider = fresh_orchestrator()
    conv_id = str(uuid4())
    orchestrator.handle_chat_message(
        "first", conv_id, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    orchestrator.handle_chat_message(
        "second", conv_id, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    recorded = provider.calls[1]
    # Bounded-but-below-limits history replays chronologically and in
    # full: the stored pair, the current user turn, then the instruction.
    # (The reply to "second" is not in history yet at call time.)
    assert [m.content for m in recorded[:-1]] == [
        "first", "Direct reply.", "second",
    ]
    assert [m.role for m in recorded[:-1]] == ["user", "assistant", "user"]
    assert recorded[-1].content.startswith(ORCHESTRATOR_INSTRUCTION_PREFIX)


def test_character_budget_binding_through_orchestrator():
    orchestrator, store, provider = fresh_orchestrator()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    fill_history(store, conv_id, 30, content="o" * 1000)

    orchestrator.handle_chat_message(
        "c" * 100, conv_id, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    window = provider.calls[0][:-1]
    assert len(window) == 12  # current 100 + 11 x 1000 chars = 11,100
    assert sum(len(m.content) for m in window) == 11 * 1000 + 100
    assert sum(len(m.content) for m in window) <= MAX_HISTORY_REPLAY_CHARS
    assert provider.calls[0][-1].content.startswith(ORCHESTRATOR_INSTRUCTION_PREFIX)


def test_tool_action_summary_call_receives_bounded_window_too():
    """The second provider call (tool result summary) is bounded equally.

    Tool handling is preserved: the summary instruction still rides on
    top of the same bounded history window.
    """
    store = PersistentConversationStore()
    provider = ScriptedProvider([DECISION_SYSTEM_INFO, "irrelevant summary"])
    approvals = ApprovalService(
        registry=tool_registry,
        executor=ToolExecutor(tool_registry),
        store=ApprovalStore(),
    )
    orchestrator = ChatOrchestrator(
        provider_factory=lambda: provider,
        approvals=approvals,
        plans=PlanService(registry=tool_registry, approvals=approvals),
        registry=tool_registry,
        conversations=store,
    )
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    for i in range(40):
        store.add_turn(
            conv_id, "user" if i % 2 else "assistant", f"t{i}",
            owner_id=OWNER_ID, session_id=SESSION_ID,
        )

    orchestrator.handle_chat_message(
        "current", conv_id, owner_id=OWNER_ID, session_id=SESSION_ID
    )

    assert len(provider.calls) == 2
    for call in provider.calls:
        window = call[:-1]
        assert len(window) == MAX_HISTORY_REPLAY_TURNS
        assert window[0].content == "t17"  # 41 stored turns -> newest 24
        assert window[-1].content == "current"
        assert call[-1].content.startswith(ORCHESTRATOR_INSTRUCTION_PREFIX)


def test_bounded_retrieval_leaves_persistent_history_unchanged():
    orchestrator, store, provider = fresh_orchestrator()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    fill_history(store, conv_id, 40, content="o" * 1000)
    before = [
        (r.conversation_id, r.seq, r.role, r.content, r.created_at)
        for r in store.execution_db.list_conversation_messages(conv_id)
    ]

    orchestrator.handle_chat_message(
        "current", conv_id, owner_id=OWNER_ID, session_id=SESSION_ID
    )

    after = [
        (r.conversation_id, r.seq, r.role, r.content, r.created_at)
        for r in store.execution_db.list_conversation_messages(conv_id)
    ]
    # Nothing truncated or rewritten: pre-existing rows compare equal
    # verbatim and only the expected current user turn + reply were added.
    assert after[: len(before)] == before
    assert len(after) == len(before) + 2
    assert after[-2][3] == "current"      # the just-stored user message
    assert after[-1][2] == "assistant"    # the assistant reply


def test_selection_cannot_bypass_ownership_checks():
    """The window is built from identity-checked history only; a foreign
    identity still fails before any history is read or selected."""
    orchestrator, store, provider = fresh_orchestrator()
    conv_id = store.create_conversation(owner_id=OWNER_ID, session_id=SESSION_ID)
    store.add_turn(
        conv_id, "user", "private", owner_id=OWNER_ID, session_id=SESSION_ID
    )

    with pytest.raises(ConversationNotFoundError):
        orchestrator.handle_chat_message(
            "peek", conv_id,
            owner_id="replay-foreign-owner", session_id=SESSION_ID,
        )
    assert provider.calls == []
