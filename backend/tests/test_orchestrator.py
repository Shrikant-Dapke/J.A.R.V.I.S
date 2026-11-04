"""Phase 4 orchestrator service tests (no HTTP, no provider network).

All provider calls are scripted locally. Approval stores, plan stores,
and conversation stores are isolated per test. Execution counting keeps
the "no accidental duplicate execution" guarantee testable at the
service level; the approval workflow stays the only execution boundary.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.schemas.approval import ApprovalStatus
from app.schemas.tool import ToolDefinition
from app.services import open_application_tool
from app.services.approval_service import (
    ApprovalService,
    ApprovalWorkflowError,
)
from app.services.approval_store import (
    ApprovalNotFoundError,
    ApprovalStateError,
    ApprovalStore,
)
from app.services.conversation_store import (
    ConversationNotFoundError,
    PersistentConversationStore,
)
from app.services.llm.base import LLMProvider
from app.services.llm.errors import (
    LLMInvalidResponseError,
    LLMProviderError,
    LLMTimeoutError,
)
from app.services.orchestrator import (
    MAX_INSTRUCTION_LENGTH,
    MAX_USER_REQUEST_SLOT,
    ORCHESTRATOR_INSTRUCTION_PREFIX,
    USER_REQUEST_MARKER,
    ChatOrchestrator,
    OrchestrationInstructionBudgetError,
    OrchestrationInvalidProposalError,
)
from app.services.plan_service import PlanService
from app.services.system_info_tool import SystemInfoTool
from app.services.tool_executor import ToolExecutor
from app.services.tool_registry import tool_registry


OWNER_ID = "orchestrator-owner-hash"
SESSION_ID = "orchestrator-session-0123456789"
OTHER_OWNER_ID = "orchestrator-other-owner"
OTHER_SESSION_ID = "orchestrator-other-sess-012345"

PLACEHOLDER_TEXT = "JARVIS assistant core is online."

DECISION_REPLY = '{"action": "reply", "reply": "Direct reply."}'
DECISION_SYSTEM_INFO = (
    '{"action": "tool_action", "tool": "get_system_info", '
    '"arguments": {}, "explanation": "Reading system info."}'
)
DECISION_OPEN_NOTEPAD = (
    '{"action": "tool_action", "tool": "open_application", '
    '"arguments": {"app_id": "notepad"}, "explanation": "Opening notepad."}'
)
SUMMARY_TEXT = "Here is what the tool reported."
PLAN_DECISION = (
    '{"action": "plan", "goal": "Show info then open notepad", '
    '"explanation": "Two actions.", "steps": ['
    '{"step_id": "s1", "description": "Get system info", '
    '"tool_name": "get_system_info", "arguments": {}, "depends_on": []}, '
    '{"step_id": "s2", "description": "Open notepad", '
    '"tool_name": "open_application", "arguments": {"app_id": "notepad"}, '
    '"depends_on": ["s1"]}]}'
)

# Metadata-only definition used by the policy-denial test. It has no
# implementation anywhere, so it can never execute even if policy were
# bypassed. Production code never imports this test module.
DENIED_TOOL_NAME = "chat_denied_stub"

if not tool_registry.exists(DENIED_TOOL_NAME):
    tool_registry.register(
        ToolDefinition(
            name=DENIED_TOOL_NAME,
            description="Test-only policy-denied tool (never executable).",
            read_only=False,
            requires_approval=False,
        )
    )

# Metadata-only READ-ONLY definition: policy allows it, but no executable
# implementation is bound, so the executor raises ToolNotImplementedError.
UNIMPLEMENTED_TOOL_NAME = "stub_readonly_tool"

if not tool_registry.exists(UNIMPLEMENTED_TOOL_NAME):
    tool_registry.register(
        ToolDefinition(
            name=UNIMPLEMENTED_TOOL_NAME,
            description="Test-only read-only metadata stub (no implementation).",
            read_only=True,
            requires_approval=False,
        )
    )


class ScriptedProvider(LLMProvider):
    """Provider returning queued responses (str or Exception)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def generate_response(self, messages):
        from app.schemas.llm import LLMResponse

        self.calls.append(list(messages))
        if len(self.calls) > len(self._responses):
            raise AssertionError("unexpected extra provider call")
        item = self._responses[len(self.calls) - 1]
        if isinstance(item, Exception):
            raise item
        return LLMResponse(content=item, model="scripted")


def fresh_orchestrator(provider):
    """Build an orchestrator with isolated stores and fresh state."""
    approvals = ApprovalService(
        registry=tool_registry,
        executor=ToolExecutor(tool_registry),
        store=ApprovalStore(),
    )
    plans = PlanService(registry=tool_registry, approvals=approvals)
    conversations = PersistentConversationStore()
    orchestrator = ChatOrchestrator(
        provider_factory=lambda: provider,
        approvals=approvals,
        plans=plans,
        registry=tool_registry,
        conversations=conversations,
    )
    return orchestrator, approvals, plans, conversations


def test_non_decision_text_becomes_plain_reply():
    """Provider text without a decision object is a safe reply fallback."""
    provider = ScriptedProvider([PLACEHOLDER_TEXT])
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)

    response = orchestrator.handle_chat_message(
        "Hello", owner_id=OWNER_ID, session_id=SESSION_ID
    )

    assert response.status == "success"
    assert response.response == PLACEHOLDER_TEXT
    assert response.action is None
    assert response.plan is None
    assert response.tool_result is None
    assert len(provider.calls) == 1
    # The decision instruction is appended after the conversation history.
    last_message = provider.calls[0][-1]
    assert ORCHESTRATOR_INSTRUCTION_PREFIX in last_message.content
    assert "USER REQUEST:" in last_message.content


def test_structured_reply_decision_is_stored():
    """A valid reply decision is returned and stored as an assistant turn."""
    provider = ScriptedProvider([DECISION_REPLY])
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)
    conv_id = str(uuid.uuid4())

    response = orchestrator.handle_chat_message(
        "Say something", conv_id, owner_id=OWNER_ID, session_id=SESSION_ID
    )

    assert response.status == "success"
    assert response.response == "Direct reply."
    turns = conversations.get_history(
        conv_id, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    assert [(t.role, t.content) for t in turns] == [
        ("user", "Say something"),
        ("assistant", "Direct reply."),
    ]


def test_decision_instruction_contains_registered_tools():
    """The decision prompt exposes the real tool catalog, not free text."""
    provider = ScriptedProvider([DECISION_REPLY])
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)

    orchestrator.handle_chat_message(
        "Hello", owner_id=OWNER_ID, session_id=SESSION_ID
    )

    instruction = provider.calls[0][-1].content
    for tool_name in ("get_system_info", "open_application", "create_directory"):
        assert tool_name in instruction


def test_valid_read_only_proposal_executes_once():
    """A registered read-only tool executes exactly once and is summarized."""
    provider = ScriptedProvider([DECISION_SYSTEM_INFO, SUMMARY_TEXT])
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)
    executions = []
    original_execute = SystemInfoTool.execute

    def counting_execute(self, arguments):
        executions.append(arguments)
        return original_execute(self, arguments)

    with patch.object(SystemInfoTool, "execute", counting_execute):
        response = orchestrator.handle_chat_message(
            "What are my system specs?",
            owner_id=OWNER_ID,
            session_id=SESSION_ID,
        )

    assert response.status == "success"
    assert response.response == SUMMARY_TEXT
    assert response.tool_result is not None
    assert response.tool_result.tool_name == "get_system_info"
    assert response.tool_result.success is True
    assert len(executions) == 1
    assert len(provider.calls) == 2


def test_malformed_decision_is_rejected_without_execution():
    """Decision-shaped but invalid output fails closed (422 semantics)."""
    provider = ScriptedProvider(
        ['{"action": "tool_action", "tool": "not an id!", "arguments": {}}']
    )
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)
    execute = Mock()
    approvals._executor.execute = execute

    with pytest.raises(OrchestrationInvalidProposalError):
        orchestrator.handle_chat_message(
            "Do it", owner_id=OWNER_ID, session_id=SESSION_ID
        )

    execute.assert_not_called()
    assert len(provider.calls) == 1


def test_unknown_action_type_is_rejected():
    """An action discriminator the orchestrator does not know is rejected."""
    provider = ScriptedProvider(['{"action": "teleport", "target": "moon"}'])
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)

    with pytest.raises(OrchestrationInvalidProposalError):
        orchestrator.handle_chat_message(
            "Beam me up", owner_id=OWNER_ID, session_id=SESSION_ID
        )


def test_unknown_tool_proposal_is_rejected():
    """Proposals naming unregistered tools are rejected before execution."""
    provider = ScriptedProvider(
        [
            '{"action": "tool_action", "tool": "delete_system32", '
            '"arguments": {"something": "x"}}'
        ]
    )
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)
    execute = Mock()
    approvals._executor.execute = execute

    with pytest.raises(OrchestrationInvalidProposalError):
        orchestrator.handle_chat_message(
            "Clean my disk", owner_id=OWNER_ID, session_id=SESSION_ID
        )

    execute.assert_not_called()
    assert len(provider.calls) == 1


def test_malformed_arguments_on_allowed_tool_return_tool_failure():
    """Allowed-path validation stays inside the tool boundary (no 500s)."""
    provider = ScriptedProvider(
        [
            '{"action": "tool_action", "tool": "get_system_info", '
            '"arguments": {"bogus": "1"}}',
            "The tool could not complete the requested operation.",
        ]
    )
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)

    response = orchestrator.handle_chat_message(
        "System info", owner_id=OWNER_ID, session_id=SESSION_ID
    )

    # The executor validated arguments and refused execution honestly.
    assert response.status == "success"
    assert response.tool_result.success is False
    assert response.tool_result.error_code == "invalid_arguments"


def test_malformed_arguments_never_create_mutating_approval(monkeypatch):
    """Arguments outside the tool schema cannot create an approval."""
    provider = ScriptedProvider([PLACEHOLDER_TEXT])
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)
    execute = Mock()
    approvals._executor.execute = execute
    popen = Mock()
    monkeypatch.setattr(open_application_tool, "WINDOWS_PLATFORM", True)
    monkeypatch.setattr(open_application_tool.subprocess, "Popen", popen)

    from app.services.approval_service import ApprovalArgumentsError

    with pytest.raises(ApprovalArgumentsError):
        approvals.request_tool(
            "open_application",
            {"app_id": "notepad & whoami"},
            OWNER_ID,
            SESSION_ID,
        )

    execute.assert_not_called()
    popen.assert_not_called()


def test_mutating_proposal_creates_pending_approval(monkeypatch):
    """Mutating proposals produce exact pending approvals, never execution."""
    provider = ScriptedProvider([DECISION_OPEN_NOTEPAD])
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)
    popen = Mock()
    monkeypatch.setattr(open_application_tool, "WINDOWS_PLATFORM", True)
    monkeypatch.setattr(open_application_tool.subprocess, "Popen", popen)

    response = orchestrator.handle_chat_message(
        "Open notepad please", owner_id=OWNER_ID, session_id=SESSION_ID
    )

    assert response.status == "action_proposed"
    assert response.action is not None
    assert response.action.tool_name == "open_application"
    assert response.action.arguments == {"app_id": "notepad"}
    assert response.action.approval_status is ApprovalStatus.PENDING
    assert response.tool_result is None
    popen.assert_not_called()

    stored = approvals.get_request(
        response.action.approval_id, OWNER_ID, SESSION_ID
    )
    assert stored.arguments == {"app_id": "notepad"}


def test_approval_binds_exact_arguments(monkeypatch):
    """Approving executes the action once, exactly as proposed."""
    provider = ScriptedProvider([DECISION_OPEN_NOTEPAD])
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)
    popen = Mock()
    monkeypatch.setattr(open_application_tool, "WINDOWS_PLATFORM", True)
    monkeypatch.setattr(open_application_tool.subprocess, "Popen", popen)

    response = orchestrator.handle_chat_message(
        "Open notepad please", owner_id=OWNER_ID, session_id=SESSION_ID
    )
    approval_id = response.action.approval_id

    approvals.approve_request(approval_id, OWNER_ID, SESSION_ID)
    with pytest.raises(ApprovalStateError):
        approvals.approve_request(approval_id, OWNER_ID, SESSION_ID)

    popen.assert_called_once_with(
        [open_application_tool.APPLICATION_ALLOWLIST["notepad"]], shell=False
    )


def test_foreign_session_cannot_approve_chat_proposal():
    """Approval ownership follows the chat session that created it."""
    provider = ScriptedProvider([DECISION_OPEN_NOTEPAD])
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)

    response = orchestrator.handle_chat_message(
        "Open notepad", owner_id=OWNER_ID, session_id=SESSION_ID
    )
    approval_id = response.action.approval_id

    with pytest.raises(ApprovalNotFoundError):
        approvals.get_request(approval_id, OTHER_OWNER_ID, OTHER_SESSION_ID)
    with pytest.raises(ApprovalNotFoundError):
        approvals.approve_request(approval_id, OTHER_OWNER_ID, OTHER_SESSION_ID)


def test_policy_denied_proposal_fails_closed():
    """A registered-but-unsafe classification denies without executing."""
    provider = ScriptedProvider(
        ['{"action": "tool_action", "tool": "chat_denied_stub", "arguments": {}}']
    )
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)
    execute = Mock()
    approvals._executor.execute = execute

    with pytest.raises(ApprovalWorkflowError):
        orchestrator.handle_chat_message(
            "Run the denied stub", owner_id=OWNER_ID, session_id=SESSION_ID
        )

    execute.assert_not_called()


def test_provider_failure_on_decision_call_propagates():
    """Decision-call provider errors keep prior error semantics."""
    provider = ScriptedProvider([LLMTimeoutError("slow")])
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)
    conv_id = str(uuid.uuid4())

    with pytest.raises(LLMTimeoutError):
        orchestrator.handle_chat_message(
            "Hello", conv_id, owner_id=OWNER_ID, session_id=SESSION_ID
        )

    history = conversations.get_history(
        conv_id, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    assert [turn.role for turn in history] == ["user"]


def test_provider_failure_maps_to_provider_error():
    """Connection-type provider failures propagate as LLMProviderError."""
    provider = ScriptedProvider([LLMProviderError("boom")])
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)

    with pytest.raises(LLMProviderError):
        orchestrator.handle_chat_message(
            "Hello", owner_id=OWNER_ID, session_id=SESSION_ID
        )


def test_summary_failure_degrades_to_tool_message():
    """A failed summary call never hides an executed tool's outcome."""
    provider = ScriptedProvider(
        [DECISION_SYSTEM_INFO, LLMInvalidResponseError("empty summary")]
    )
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)

    response = orchestrator.handle_chat_message(
        "System info please", owner_id=OWNER_ID, session_id=SESSION_ID
    )

    assert response.status == "success"
    assert response.tool_result.success is True
    assert response.response == "System information retrieved."


def test_fenced_json_decision_is_parsed():
    """Decision JSON wrapped in markdown fences is still understood."""
    provider = ScriptedProvider(
        ['```json\n{"action": "reply", "reply": "Fence reply."}\n```']
    )
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)

    response = orchestrator.handle_chat_message(
        "Hello", owner_id=OWNER_ID, session_id=SESSION_ID
    )

    assert response.status == "success"
    assert response.response == "Fence reply."


def test_one_decision_executes_exactly_one_tool():
    """A single tool_action decision maps to a single tool execution."""
    provider = ScriptedProvider([DECISION_SYSTEM_INFO, SUMMARY_TEXT])
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)
    executions = []
    original_execute = SystemInfoTool.execute

    def counting_execute(self, arguments):
        executions.append(arguments)
        return original_execute(self, arguments)

    with patch.object(SystemInfoTool, "execute", counting_execute):
        orchestrator.handle_chat_message(
            "System info", owner_id=OWNER_ID, session_id=SESSION_ID
        )

    assert len(executions) == 1


def test_plan_decision_is_captured_not_executed():
    """Plan proposals are validated, stored, and never executed."""
    provider = ScriptedProvider([PLAN_DECISION])
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)
    execute = Mock()
    approvals._executor.execute = execute

    response = orchestrator.handle_chat_message(
        "Show my specs, then open notepad",
        owner_id=OWNER_ID,
        session_id=SESSION_ID,
    )

    assert response.status == "plan_proposed"
    assert response.plan is not None
    assert len(response.plan.steps) == 2
    assert response.plan.steps[0].status.value == "PENDING"
    assert response.plan.steps[0].depends_on == []
    assert response.plan.steps[1].depends_on == ["s1"]
    assert response.plan.conversation_id is not None
    execute.assert_not_called()
    assert len(provider.calls) == 1

    ready = plans.ready_step_ids(response.plan.plan_id, OWNER_ID, SESSION_ID)
    assert ready == ["s1"]


def test_plan_with_unknown_tool_is_rejected():
    """A plan referencing an unregistered tool is rejected (fail closed)."""
    provider = ScriptedProvider(
        [
            '{"action": "plan", "goal": "Impossible", "steps": ['
            '{"step_id": "s1", "description": "Use unknown tool", '
            '"tool_name": "no_such_tool", "arguments": {}, '
            '"depends_on": []}]}'
        ]
    )
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)
    execute = Mock()
    approvals._executor.execute = execute

    with pytest.raises(OrchestrationInvalidProposalError):
        orchestrator.handle_chat_message(
            "Do the impossible", owner_id=OWNER_ID, session_id=SESSION_ID
        )

    execute.assert_not_called()


def test_foreign_conversation_is_never_reused():
    """A caller-supplied conversation owned by another identity raises."""
    provider = ScriptedProvider([DECISION_REPLY])
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)
    conv_id = str(uuid.uuid4())

    orchestrator.handle_chat_message(
        "First", conv_id, owner_id=OWNER_ID, session_id=SESSION_ID
    )
    with pytest.raises(ConversationNotFoundError):
        orchestrator.handle_chat_message(
            "Hijack",
            conv_id,
            owner_id=OTHER_OWNER_ID,
            session_id=OTHER_SESSION_ID,
        )
    assert len(provider.calls) == 1


# --- unimplemented registered tools (M3) -----------------------------------


def test_unimplemented_tool_proposal_is_rejected_without_execution():
    """A registered read-only stub fails closed as an invalid proposal.

    Uses the real executor: the registered metadata has no bound
    implementation, so policy allows the proposal but the executor
    boundary refuses it, and the orchestrator turns that into a
    structured invalid-proposal error instead of a 500.
    """
    provider = ScriptedProvider(
        [
            '{"action": "tool_action", "tool": "stub_readonly_tool", '
            '"arguments": {}}'
        ]
    )
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)

    with pytest.raises(OrchestrationInvalidProposalError):
        orchestrator.handle_chat_message(
            "Use the stub", owner_id=OWNER_ID, session_id=SESSION_ID
        )

    assert len(provider.calls) == 1  # no summary call, no second turn


# --- decision-instruction budget (M4) ---------------------------------------


def test_oversized_request_fails_closed_before_provider_call():
    """A request beyond the reserved slot is rejected, never truncated."""
    provider = ScriptedProvider([])
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)

    oversized = "x" * (MAX_USER_REQUEST_SLOT + 1)
    with pytest.raises(OrchestrationInstructionBudgetError):
        orchestrator.handle_chat_message(
            oversized, owner_id=OWNER_ID, session_id=SESSION_ID
        )

    assert provider.calls == []  # the model never sees a truncated request


def test_instruction_budget_exactly_fits():
    """A request exactly within the slot composes a full instruction."""
    provider = ScriptedProvider([DECISION_REPLY])
    orchestrator, approvals, plans, conversations = fresh_orchestrator(provider)
    message = "y" * MAX_USER_REQUEST_SLOT

    orchestrator.handle_chat_message(
        message, owner_id=OWNER_ID, session_id=SESSION_ID
    )

    instruction = provider.calls[0][-1].content
    assert len(instruction) <= MAX_INSTRUCTION_LENGTH
    assert instruction.endswith(message)  # request arrives complete


def test_catalog_budget_drift_fails_closed(monkeypatch):
    """Trusted metadata outgrowing the budget fails closed, never truncates.

    The drift is forced by widening the catalog budget so the bounded
    head + catalog + reserved user-request slot can no longer fit the
    fixed instruction length — the invariant check must reject the
    instruction instead of silently cutting the user request.
    """
    from app.services import orchestrator as orchestrator_module

    big = [
        SimpleNamespace(
            definition=SimpleNamespace(
                name=f"tool_{index}",
                description="d" * 512,
                requires_approval=False,
                input_schema={"type": "object"},
            )
        )
        for index in range(8)
    ]
    registry = SimpleNamespace(list_tools=lambda: big)
    provider = ScriptedProvider([])
    approvals = ApprovalService(
        registry=tool_registry,
        executor=ToolExecutor(tool_registry),
        store=ApprovalStore(),
    )
    orchestrator = ChatOrchestrator(
        provider_factory=lambda: provider,
        approvals=approvals,
        registry=registry,
    )
    monkeypatch.setattr(orchestrator_module, "MAX_TOOL_CATALOG_LENGTH", 3000)

    with pytest.raises(OrchestrationInstructionBudgetError):
        orchestrator.handle_chat_message(
            "Hello", owner_id=OWNER_ID, session_id=SESSION_ID
        )

    assert provider.calls == []


def test_catalog_overflow_drops_whole_lines_with_note():
    """Catalog lines are dropped whole when the listing outgrows budget."""
    big = [
        SimpleNamespace(
            definition=SimpleNamespace(
                name=f"tool_{index}",
                description="d" * 300,
                requires_approval=False,
                input_schema=None,
            )
        )
        for index in range(8)
    ]
    registry = SimpleNamespace(list_tools=lambda: big)
    provider = ScriptedProvider([DECISION_REPLY])
    approvals = ApprovalService(
        registry=tool_registry,
        executor=ToolExecutor(tool_registry),
        store=ApprovalStore(),
    )
    orchestrator = ChatOrchestrator(
        provider_factory=lambda: provider,
        approvals=approvals,
        registry=registry,
    )
    orchestrator.handle_chat_message(
        "Hello", owner_id=OWNER_ID, session_id=SESSION_ID
    )

    instruction = provider.calls[0][-1].content
    assert len(instruction) <= MAX_INSTRUCTION_LENGTH
    assert "(additional registered tools omitted)" in instruction
    assert ORCHESTRATOR_INSTRUCTION_PREFIX in instruction
    assert USER_REQUEST_MARKER in instruction
    assert "Hello" in instruction
