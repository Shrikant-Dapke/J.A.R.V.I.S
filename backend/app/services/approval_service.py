"""Policy-aware approval workflow between proposals and tool execution."""

from typing import Any, Mapping, Union

from app.core.logging import get_logger
from app.schemas.approval import ApprovalRequest
from app.schemas.policy import PolicyDecision
from app.schemas.tool import ToolResult
from app.services.approval_store import (
    ApprovalStore,
    approval_store,
)
from app.services.policy_service import evaluate_tool_policy
from app.services.tool_executor import ToolExecutor, default_tool_executor
from app.services.tool_base import Tool
from app.services.tool_registry import ToolRegistry, UnknownToolError, tool_registry


logger = get_logger(__name__)
ApprovalWorkflowResult = Union[ToolResult, ApprovalRequest]


class ApprovalWorkflowError(ValueError):
    """Safe service error that can be mapped to a structured HTTP response."""


class ApprovalPolicyError(ApprovalWorkflowError):
    """Raised when an approval no longer satisfies current policy."""


class ApprovalIntegrityError(ApprovalWorkflowError):
    """Raised when stored arguments no longer match the tool schema exactly."""


class ApprovalArgumentsError(ApprovalWorkflowError):
    """Raised when a proposed action cannot pass the tool input schema."""


class ApprovalService:
    """Coordinate policy, approval state, and the already-authorized executor."""

    def __init__(
        self,
        registry: ToolRegistry = tool_registry,
        executor: ToolExecutor = default_tool_executor,
        store: ApprovalStore = approval_store,
    ) -> None:
        self._registry = registry
        self._executor = executor
        self._store = store

    @staticmethod
    def _action_description(
        tool_name: str, description: str, arguments: Mapping[str, Any]
    ) -> str:
        """Build a bounded description from already validated arguments."""
        if not arguments:
            return f"{tool_name}: {description}"
        rendered = ", ".join(
            f"{key}={value}" for key, value in sorted(arguments.items())
        )
        return f"{tool_name}: {description} ({rendered})"[:512]

    def _decision(self, tool_name: str) -> PolicyDecision:
        return evaluate_tool_policy(tool_name, registry=self._registry)

    def _normalized_arguments(
        self, tool_name: str, arguments: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """Validate a proposal without crossing the execution boundary."""
        registered = self._registry.require(tool_name)
        if not isinstance(registered, Tool):
            raise ApprovalArgumentsError("The requested tool is not executable.")
        return registered.normalized_arguments(arguments)

    def request_tool(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> ApprovalWorkflowResult:
        """Execute read-only tools or create a pending exact approval."""
        decision = self._decision(tool_name)
        if self._registry.get(tool_name) is None:
            raise UnknownToolError(f"Tool '{tool_name}' is not registered")

        if decision.allowed:
            return self._executor.execute(tool_name, arguments)

        if not decision.requires_approval or decision.risk_level is None:
            raise ApprovalWorkflowError(decision.reason)

        definition = self._registry.get_definition(tool_name)
        if definition is None:
            raise UnknownToolError(f"Tool '{tool_name}' is not registered")

        try:
            normalized = self._normalized_arguments(tool_name, arguments)
        except ValueError as exc:
            raise ApprovalArgumentsError("The tool arguments are invalid.") from exc

        request = self._store.create_approval_request(
            tool_name=tool_name,
            arguments={key: str(value) for key, value in normalized.items()},
            action_description=self._action_description(
                tool_name, definition.description, normalized
            ),
            risk_level=decision.risk_level,
        )
        return request

    def get_request(self, approval_id: str) -> ApprovalRequest:
        """Return approval state with lazy expiration applied."""
        return self._store.get_approval_request(approval_id)

    def reject_request(self, approval_id: str) -> ApprovalRequest:
        """Reject a pending request without reaching the executor."""
        return self._store.reject_request(approval_id)

    def approve_request(self, approval_id: str) -> ToolResult:
        """Authorize, consume, and execute exactly one approved request."""
        request = self._store.get_approval_request(approval_id)
        decision = self._decision(request.tool_name)
        if decision.allowed or not decision.requires_approval:
            raise ApprovalPolicyError(
                "The approval no longer satisfies the current tool policy."
            )
        if decision.risk_level != request.risk_level:
            raise ApprovalPolicyError(
                "The approval risk classification no longer matches policy."
            )

        try:
            normalized = self._normalized_arguments(
                request.tool_name, request.arguments
            )
        except ValueError as exc:
            raise ApprovalIntegrityError(
                "The approved arguments are no longer valid."
            ) from exc

        normalized_strings = {
            key: str(value) for key, value in normalized.items()
        }
        if normalized_strings != request.arguments:
            raise ApprovalIntegrityError(
                "The approved arguments no longer match the original request."
            )

        self._store.approve_request(approval_id)
        consumed = self._store.claim_approved_for_execution(approval_id)
        try:
            return self._executor.execute(consumed.tool_name, consumed.arguments)
        except ValueError as exc:
            logger.warning(
                "Approved tool had no executable implementation",
                extra={
                    "extra_fields": {
                        "tool_name": consumed.tool_name,
                        "error_type": type(exc).__name__,
                    }
                },
            )
            return ToolResult.failure_result(
                tool_name=consumed.tool_name,
                error_code="tool_unavailable",
                message="The approved tool is not available for execution.",
            )


approval_service = ApprovalService()
