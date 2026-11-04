"""Main orchestrator: connects chat, LLM, policy, approval, and tools.

Phase 4 milestone 4.1. One controlled orchestrator sits behind the
existing chat flow. It:

1. Stores the user turn under the authenticated identity (conversation
   ownership and session boundaries are preserved from Phase 2/3).
2. Calls the configured LLM provider with a bounded replay window of the
   conversation history — the newest COMPLETE turns that fit the
   configured turn and character budgets; persisted history itself is
   never rewritten or truncated — plus a trusted orchestration
   instruction to obtain a structured decision.
3. Validates the decision strictly (model output is untrusted):
   - ``reply`` — plain conversational answer.
   - ``tool_action`` — ONE registered tool with bounded string args.
   - ``plan`` — a bounded multi-step plan (captured, never executed).
4. Forwards tool proposals through the EXISTING policy and approval
   workflow (``approval_service.request_tool``). Read-only tools allowed
   by policy execute once; mutating tools produce a pending approval
   bound to the exact validated arguments and execute nothing here.
5. Feeds executed tool results back to the provider for a factual
   user-facing summary.
6. Returns a structured ChatResponse to the existing chat API.

The model never executes tools and never authorizes its own actions:
only ``policy_service`` and ``approval_service`` may authorize, and only
the existing executor boundary may run a registered tool.

Security notes:
- Decision output that claims to be a decision but fails validation is
  rejected (structured 422); text that is not decision-shaped stays
  plain conversation and is never interpreted.
- Approval-store unavailability never degrades into execution.
- Logs carry decision kind, tool name, and outcome status only — never
  user content, model text, arguments, tokens, or session identifiers.
"""

import json
import uuid
from typing import Callable, Mapping, Optional

from app.core.logging import get_logger
from app.schemas.approval import ApprovalRequest
from app.schemas.chat import ChatResponse
from app.schemas.conversation import ConversationTurn
from app.schemas.llm import LLMMessage, LLMResponse
from app.schemas.orchestration import (
    EMPTY_TEXT_REPLY,
    ExecutedAction,
    OrchestratorDecision,
    OrchestratorDecisionParseError,
    PlanDecision,
    ReplyDecision,
    ToolActionDecision,
    ProposedAction,
    parse_orchestrator_decision,
)
from app.schemas.plan import TaskPlan
from app.schemas.tool import ToolResult
from app.services.approval_service import (
    ApprovalArgumentsError,
    ApprovalService,
    ApprovalWorkflowError,
    approval_service,
)
from app.services.approval_store import ApprovalCapacityError
from app.services.conversation_store import (
    PersistentConversationStore,
    conversation_store,
)
from app.services.llm.base import LLMProvider
from app.services.llm.errors import LLMError
from app.services.llm.factory import get_llm_provider
from app.services.plan_service import (
    PlanCapacityError,
    PlanService,
    PlanValidationError,
    plan_service,
)
from app.services.tool_executor import ToolExecutor, ToolNotImplementedError
from app.services.tool_registry import (
    ToolRegistry,
    UnknownToolError,
    tool_registry,
)


logger = get_logger(__name__)

# Assistant-visible text is bounded to the LLM message contract so every
# stored turn can always be replayed into a later provider call.
MAX_RESPONSE_TEXT_LENGTH = 4000

# Decision-instruction budget: the instruction is composed from trusted
# parts whose sizes are bounded SEPARATELY (tool catalog vs. user-request
# slot). The complete user request always reaches the model; when any
# part would not fit the budget, the orchestrator fails closed instead
# of silently truncating content.
MAX_INSTRUCTION_LENGTH = 4000
MAX_USER_REQUEST_SLOT = 1500
MAX_TOOL_CATALOG_LENGTH = 1300

# Provider replay budget (Phase 5.1): every conversational provider call
# replays at most the newest 24 complete turns and at most 12,000
# characters of history, regardless of how large the persisted
# conversation grows. The budgets bound the PROMPT only; stored history
# is never cut, rewritten, or summarized here.
MAX_HISTORY_REPLAY_TURNS = 24
MAX_HISTORY_REPLAY_CHARS = 12000

ORCHESTRATOR_INSTRUCTION_PREFIX = "[ORCHESTRATOR INSTRUCTION - JARVIS]"
USER_REQUEST_MARKER = "USER REQUEST:"


class OrchestrationInvalidProposalError(ValueError):
    """A proposal-bearing decision failed validation and nothing executed.

    Raised for unknown tools, malformed arguments, unknown decision
    types, and rejected plan proposals. Carries only a safe, bounded
    message; raw model output is never included or logged.
    """


class OrchestrationInstructionBudgetError(ValueError):
    """The decision instruction could not be composed within its budget.

    Raised when the user request exceeds the reserved request slot, or
    when trusted instruction/catalog metadata outgrew its configured
    bounds (a configuration bug). Mapped to a structured HTTP 422 by the
    chat route; never silently truncates the user request.
    """


def _bounded_text(text: str, limit: int) -> str:
    """Return text trimmed to a hard length bound (model text is untrusted)."""
    if len(text) <= limit:
        return text
    return text[:limit]


def select_replay_window(
    history: list[ConversationTurn],
) -> list[ConversationTurn]:
    """Select the bounded provider replay window from stored history.

    Whole-turn selection policy (Phase 5.1):

    - The input is the identity-verified stored history in chronological
      order, INCLUDING the current user message as its newest element.
    - The current user message (newest turn) is always included — even
      in the theoretical case where it alone would exceed the character
      budget. It is never dropped, never truncated.
    - Older turns are then added newest-first, one WHOLE turn at a
      time, while the window stays within both budgets: at most
      ``MAX_HISTORY_REPLAY_TURNS`` turns and at most
      ``MAX_HISTORY_REPLAY_CHARS`` characters of turn content
      (hitting a budget exactly still counts as fitting).
    - The backward walk stops at the first older turn that does not
      fit entirely. The result is therefore always the newest
      CONTIGUOUS span of complete turns: the provider never sees a
      mid-history gap, and no turn is ever truncated, split, paired
      down, or silently rewritten.
    - Chronological order is restored before returning; the caller's
      list is not mutated and the store is not touched — this is pure
      post-processing of already-ownership-checked turns, so it cannot
      bypass any authentication or identity boundary.
    """
    if not history:
        return []
    current = history[-1]
    window = [current]
    used = len(current.content)
    if used <= MAX_HISTORY_REPLAY_CHARS:
        for turn in reversed(history[:-1]):
            if len(window) >= MAX_HISTORY_REPLAY_TURNS:
                break
            if used + len(turn.content) > MAX_HISTORY_REPLAY_CHARS:
                break
            window.append(turn)
            used += len(turn.content)
    window.reverse()
    return window


class ChatOrchestrator:
    """Single controlled orchestrator behind the chat service boundary."""

    def __init__(
        self,
        *,
        provider_factory: Optional[Callable[[], LLMProvider]] = None,
        approvals: ApprovalService = approval_service,
        plans: PlanService = plan_service,
        registry: ToolRegistry = tool_registry,
        conversations: PersistentConversationStore = conversation_store,
    ) -> None:
        self._provider_factory = provider_factory or get_llm_provider
        self._approvals = approvals
        self._plans = plans
        self._registry = registry
        self._conversations = conversations

    # -- public entry point --------------------------------------------

    def handle_chat_message(
        self,
        message: str,
        conversation_id: Optional[str] = None,
        *,
        owner_id: str,
        session_id: str,
    ) -> ChatResponse:
        """Handle one authenticated user turn end to end."""
        resolved_id = self._store_user_turn(
            message, conversation_id, owner_id=owner_id, session_id=session_id
        )
        history = self._conversations.get_history(
            resolved_id, owner_id=owner_id, session_id=session_id
        ) or []
        # Only the newest complete turns that fit the replay budgets
        # reach the provider; the persisted history itself is untouched.
        messages = [
            LLMMessage(role=turn.role, content=turn.content)
            for turn in select_replay_window(history)
        ]

        provider = self._provider_factory()

        decision_text = self._generate(
            provider,
            messages,
            self._build_decision_instruction(message),
        )
        try:
            decision: OrchestratorDecision = parse_orchestrator_decision(
                decision_text
            )
        except OrchestratorDecisionParseError as exc:
            logger.warning(
                "Orchestrator rejected malformed decision output",
                extra={"extra_fields": {"decision": "invalid"}},
            )
            raise OrchestrationInvalidProposalError(
                "The assistant returned a decision that failed validation."
            ) from exc

        if isinstance(decision, ReplyDecision):
            return self._handle_reply(resolved_id, decision, owner_id, session_id)
        if isinstance(decision, ToolActionDecision):
            return self._handle_tool_action(
                resolved_id,
                decision,
                messages,
                provider,
                owner_id,
                session_id,
            )
        return self._handle_plan(resolved_id, decision, owner_id, session_id)

    # -- conversation helpers -------------------------------------------

    def _store_user_turn(
        self,
        message: str,
        conversation_id: Optional[str],
        *,
        owner_id: str,
        session_id: str,
    ) -> str:
        """Create/attach the conversation and append the user turn first.

        Ownership checks run before any provider call: a conversation of
        another identity raises instead of being reused or modified.
        """
        resolved_id = conversation_id.strip() if conversation_id else None
        if not resolved_id:
            resolved_id = str(uuid.uuid4())
        self._conversations.create_conversation(
            resolved_id, owner_id=owner_id, session_id=session_id
        )
        self._conversations.add_turn(
            resolved_id, "user", message, owner_id=owner_id, session_id=session_id
        )
        return resolved_id

    # -- decision branches -----------------------------------------------

    def _handle_reply(
        self,
        conversation_id: str,
        decision: ReplyDecision,
        owner_id: str,
        session_id: str,
    ) -> ChatResponse:
        reply = _bounded_text(
            decision.reply.strip() or EMPTY_TEXT_REPLY, MAX_RESPONSE_TEXT_LENGTH
        )
        self._conversations.add_turn(
            conversation_id,
            "assistant",
            reply,
            owner_id=owner_id,
            session_id=session_id,
        )
        return ChatResponse(
            conversation_id=conversation_id,
            response=reply,
            status="success",
        )

    def _handle_tool_action(
        self,
        conversation_id: str,
        decision: ToolActionDecision,
        messages: list[LLMMessage],
        provider: LLMProvider,
        owner_id: str,
        session_id: str,
    ) -> ChatResponse:
        """Forward one validated tool proposal through the approval workflow."""
        try:
            self._registry.require(decision.tool)
        except UnknownToolError as exc:
            logger.warning(
                "Orchestrator rejected an unknown tool proposal",
                extra={"extra_fields": {"tool_name": decision.tool}},
            )
            raise OrchestrationInvalidProposalError(
                "The assistant proposed a tool that is not registered."
            ) from exc

        try:
            outcome = self._approvals.request_tool(
                decision.tool,
                dict(decision.arguments),
                owner_id,
                session_id,
            )
        except (
            ApprovalArgumentsError,
            ApprovalCapacityError,
            ApprovalWorkflowError,
        ):
            # Approval-store failures must never degrade into execution:
            # nothing has run and the caller receives a structured error.
            raise
        except UnknownToolError as exc:
            raise OrchestrationInvalidProposalError(
                "The assistant proposed a tool that is not registered."
            ) from exc
        except ToolNotImplementedError as exc:
            logger.warning(
                "Orchestrator rejected a registered tool without an implementation",
                extra={"extra_fields": {"tool_name": decision.tool}},
            )
            raise OrchestrationInvalidProposalError(
                "The assistant proposed a tool that is not ready for execution."
            ) from exc

        if isinstance(outcome, ToolResult):
            return self._respond_executed(
                conversation_id,
                outcome,
                messages,
                provider,
                owner_id,
                session_id,
            )

        # Pending approval: the workflow has bound the exact validated
        # arguments; the orchestrator returns it untouched and executes
        # nothing until the user approves.
        return self._respond_proposed(
            conversation_id, decision, outcome, owner_id, session_id
        )

    def _respond_executed(
        self,
        conversation_id: str,
        result: ToolResult,
        messages: list[LLMMessage],
        provider: LLMProvider,
        owner_id: str,
        session_id: str,
    ) -> ChatResponse:
        """Summarize an executed tool result and store the assistant turn."""
        reply = self._summarize_result(provider, messages, result)

        self._conversations.add_turn(
            conversation_id,
            "assistant",
            reply,
            owner_id=owner_id,
            session_id=session_id,
        )
        logger.info(
            "Orchestrator executed an allowed tool via the approval workflow",
            extra={
                "extra_fields": {
                    "tool_name": result.tool_name,
                    "tool_status": result.status,
                    "decision": "tool_action",
                }
            },
        )
        return ChatResponse(
            conversation_id=conversation_id,
            response=reply,
            status="success",
            tool_result=ExecutedAction(
                tool_name=result.tool_name,
                status=result.status,
                success=result.success,
                message=result.message,
                data=result.data,
                error_code=result.error_code,
            ),
        )

    def _respond_proposed(
        self,
        conversation_id: str,
        decision: ToolActionDecision,
        request: ApprovalRequest,
        owner_id: str,
        session_id: str,
    ) -> ChatResponse:
        """Report a pending approval without executing anything."""
        assistant_text = _bounded_text(
            decision.explanation or request.action_description,
            MAX_RESPONSE_TEXT_LENGTH,
        )
        self._conversations.add_turn(
            conversation_id,
            "assistant",
            assistant_text,
            owner_id=owner_id,
            session_id=session_id,
        )
        logger.info(
            "Orchestrator proposed an approval-gated action",
            extra={
                "extra_fields": {
                    "tool_name": request.tool_name,
                    "risk_level": request.risk_level.value,
                    "decision": "tool_action",
                }
            },
        )
        return ChatResponse(
            conversation_id=conversation_id,
            response=assistant_text,
            status="action_proposed",
            action=ProposedAction(
                approval_id=request.approval_id,
                tool_name=request.tool_name,
                arguments=request.arguments,
                action_description=request.action_description,
                risk_level=request.risk_level,
                approval_status=request.status,
                expires_at=request.expires_at,
            ),
        )

    def _handle_plan(
        self,
        conversation_id: str,
        decision: PlanDecision,
        owner_id: str,
        session_id: str,
    ) -> ChatResponse:
        """Validate and capture a bounded plan; execute nothing."""
        try:
            plan: TaskPlan = self._plans.create_plan(
                goal=decision.goal,
                steps=[step.model_dump() for step in decision.steps],
                owner_id=owner_id,
                session_id=session_id,
                conversation_id=conversation_id,
            )
        except PlanValidationError as exc:
            logger.warning(
                "Orchestrator rejected an invalid plan proposal",
                extra={"extra_fields": {"error_type": type(exc).__name__}},
            )
            raise OrchestrationInvalidProposalError(
                "The assistant proposed an invalid task plan."
            ) from exc

        assistant_text = _bounded_text(
            self._plan_reply_text(plan), MAX_RESPONSE_TEXT_LENGTH
        )
        self._conversations.add_turn(
            conversation_id,
            "assistant",
            assistant_text,
            owner_id=owner_id,
            session_id=session_id,
        )
        logger.info(
            "Orchestrator captured a bounded task plan",
            extra={
                "extra_fields": {
                    "decision": "plan",
                    "step_count": len(plan.steps),
                }
            },
        )
        return ChatResponse(
            conversation_id=conversation_id,
            response=assistant_text,
            status="plan_proposed",
            plan=plan,
        )

    # -- provider helpers -----------------------------------------------

    @staticmethod
    def _generate(
        provider: LLMProvider, messages: list[LLMMessage], instruction: str
    ) -> str:
        """Run one bounded provider call for an orchestration instruction.

        Provider errors (configuration, API, timeout, invalid response)
        propagate untouched so the existing chat error mapping applies.
        """
        response: LLMResponse = provider.generate_response(
            messages + [LLMMessage(role="user", content=instruction)]
        )
        return response.content

    def _summarize_result(
        self,
        provider: LLMProvider,
        messages: list[LLMMessage],
        result: ToolResult,
    ) -> str:
        """Give the tool result back to the provider for a factual reply.

        If the summary call fails, the honest tool-authored message is
        used as the reply: the action did execute and must not be hidden.
        """
        try:
            text = self._generate(
                provider, messages, self._build_summary_instruction(result)
            ).strip()
        except LLMError as exc:
            logger.warning(
                "Tool result summary call failed; using the tool-authored message",
                extra={
                    "extra_fields": {
                        "error_type": type(exc).__name__,
                        "tool_name": result.tool_name,
                    }
                },
            )
            text = ""
        if not text:
            text = result.message
        return _bounded_text(text, MAX_RESPONSE_TEXT_LENGTH)

    # -- instruction builders (trusted, bounded) -------------------------

    def _build_decision_instruction(self, user_message: str) -> str:
        """Compose the trusted decision instruction for the provider.

        Bounded by construction: the trusted head (rules), the tool
        catalog, and the user request each respect a separate budget.
        The user request is never truncated; an oversized request fails
        clearly (structured 422) instead of reaching the model cut off.
        """
        head = (
            f"{ORCHESTRATOR_INSTRUCTION_PREFIX}\n"
            "You are JARVIS, a desktop assistant. Decide how to handle the "
            "user request.\n\n"
            "REGISTERED TOOLS (only these may be used):\n"
        )
        tail = (
            "\n\nRULES:\n"
            "1. Your entire reply must be exactly one JSON object. No markdown, "
            "no extra text.\n"
            '2. If the request can be answered without tools: '
            '{"action": "reply", "reply": "<your answer>"}\n'
            "3. If the request needs exactly one tool action: "
            '{"action": "tool_action", "tool": "<tool_name>", '
            '"arguments": {"<argument>": "<value as string>"}, '
            '"explanation": "<one short sentence>"}\n'
            "   Only propose arguments defined by the tool's schema; values "
            "must be strings.\n"
            "4. If the request needs several tool actions in sequence: "
            '{"action": "plan", "goal": "<achieved outcome>", '
            '"explanation": "<one short sentence>", "steps": ['
            '{"step_id": "<short_id>", "description": "<what this step does>", '
            '"tool_name": "<tool>", "arguments": {...}, '
            '"depends_on": ["<earlier step_ids>"]}]}\n'
            "   Use at most 8 steps; depends_on may only reference earlier "
            "steps.\n"
            "5. Never invent tools, arguments, or results. Never include "
            "reasoning or instructions inside the JSON.\n\n"
            f"{USER_REQUEST_MARKER}\n"
        )
        catalog = self._compact_tool_catalog()
        if (
            len(head) + len(tail) + len(catalog) + MAX_USER_REQUEST_SLOT
            > MAX_INSTRUCTION_LENGTH
        ):
            raise OrchestrationInstructionBudgetError(
                "The orchestrator instruction template exceeded its configured "
                "budget."
            )
        if len(user_message) > MAX_USER_REQUEST_SLOT:
            raise OrchestrationInstructionBudgetError(
                "The user request does not fit the orchestrator instruction "
                "budget."
            )
        return f"{head}{catalog}{tail}{user_message}"

    @staticmethod
    def _build_summary_instruction(result: ToolResult) -> str:
        """Compose the trusted summary instruction from a tool result."""
        bounded_data = {
            key: _bounded_text(value, 120)
            for key, value in (result.data or {}).items()
        }
        bounded_data = dict(list(bounded_data.items())[:8])
        result_summary = {
            "tool": result.tool_name,
            "status": result.status,
            "message": result.message,
        }
        if bounded_data:
            result_summary["data"] = bounded_data
        return (
            f"{ORCHESTRATOR_INSTRUCTION_PREFIX}\n"
            "You are JARVIS. The user's request was just handled by running one "
            "registered tool.\n"
            "The result below is authoritative; do not invent extra details.\n"
            f"{json.dumps(result_summary, separators=(',', ':'))}\n\n"
            "Write a short user-facing reply (at most three sentences) reporting "
            "this outcome as plain text. No JSON. If it failed, say so plainly."
        )

    def _compact_tool_catalog(self) -> str:
        """Render the registered-tool catalog within its bounded budget.

        Lines are dropped whole (never mid-schema) when the catalog
        outgrows the budget, with a trailing note; the structural rules
        and the user request keep their full budget.
        """
        lines: list[str] = []
        for registered in self._registry.list_tools():
            definition = (
                registered.definition
                if hasattr(registered, "definition")
                else registered
            )
            schema_text = (
                self._compact_schema(definition.input_schema)
                if definition.input_schema
                else "{}"
            )
            lines.append(
                f"- {definition.name} | {definition.description} | "
                f"approval_required: "
                f"{'yes' if definition.requires_approval else 'no'} | "
                f"arguments: {schema_text}"
            )
        if not lines:
            return "(no tools are currently registered)"
        catalog = "\n".join(lines)
        if len(catalog) <= MAX_TOOL_CATALOG_LENGTH:
            return catalog
        note = "(additional registered tools omitted)"
        reserved = MAX_TOOL_CATALOG_LENGTH - len(note)
        kept: list[str] = []
        used = 0
        omitted = 0
        for line in lines:
            spend = len(line) + (1 if kept else 0)
            if used + spend > reserved:
                omitted += 1
                continue
            kept.append(line)
            used += spend
        if omitted:
            kept.append(note)
        return "\n".join(kept)

    @staticmethod
    def _compact_schema(schema: Mapping) -> str:
        """Render a tool input schema compactly and bound it."""
        try:
            return _bounded_text(json.dumps(schema, separators=(",", ":")), 600)
        except (TypeError, ValueError):
            return "{}"

    @staticmethod
    def _plan_reply_text(plan: TaskPlan) -> str:
        """Deterministic plan summary composed from validated data only."""
        lines = [
            f"I have prepared a {len(plan.steps)}-step plan: {plan.goal}",
        ]
        for index, step in enumerate(plan.steps, start=1):
            lines.append(f"{index}. {step.description}")
        lines.append(
            "Each step still goes through policy and, for mutating actions, "
            "your explicit approval before anything executes."
        )
        return "\n".join(lines)


# Module orchestrator instance used by the chat service.
chat_orchestrator = ChatOrchestrator()
