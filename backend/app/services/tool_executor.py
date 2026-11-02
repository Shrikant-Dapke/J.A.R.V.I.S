"""Execution boundary for tools that have already been authorized.

Phase 4.3: tool execution now runs under a bounded wall-clock budget
that is completely separate from provider/HTTP timeouts. The budget is
enforced here, at the execution boundary, with a plain thread join —
no new dependencies, no async rework.

Honesty rules preserved by design:

- A timeout NEVER implies the side effect is absent (or present). The
  executor returns a dedicated ``execution_timeout`` failure result so
  callers can keep the mutation boundary standing (mutating tools =>
  attempt TIMED_OUT and plan step RECOVERY_REQUIRED; read-only tools =>
  ordinary failure, since they are provably side-effect free).
- A stuck invocation keeps running detached (daemon thread) and is
  logged; its orphaned result is discarded, never reported success.
- ``timeout_seconds=None`` runs inline exactly like before — existing
  callers and tests keep their behavior unless they opt into a budget.
"""

import threading
from typing import Any, Mapping, Optional

from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.tool import ToolResult
from app.services.tool_base import Tool
from app.services.tool_registry import ToolRegistry, UnknownToolError, tool_registry
from app.services.execution_lifecycle import ERROR_EXECUTION_TIMEOUT


logger = get_logger(__name__)


class ToolNotImplementedError(ValueError):
    """Raised when metadata exists but no executable implementation is bound."""


class ToolExecutor:
    """Invoke registered implementations without making policy decisions."""

    def __init__(
        self,
        registry: ToolRegistry,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        self._registry = registry
        # ``None`` keeps the legacy inline behavior; a positive number
        # gives every execution a bounded wall-clock budget.
        self.timeout_seconds = timeout_seconds

    def execute(
        self,
        tool_name: str,
        arguments: Optional[Mapping[str, Any]] = None,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> ToolResult:
        """Execute one registered tool; callers must perform authorization first.

        ``timeout_seconds`` overrides the instance budget for this call.
        """
        registered = self._registry.require(tool_name)
        if not isinstance(registered, Tool):
            raise ToolNotImplementedError(
                f"Tool '{tool_name}' has no registered implementation"
            )
        budget = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if budget is None:
            return registered.invoke(arguments)
        if not isinstance(budget, (int, float)) or budget <= 0:
            raise ValueError("tool execution timeout must be a positive number")
        return self._execute_bounded(registered, arguments, float(budget))

    def _execute_bounded(
        self,
        registered: Tool,
        arguments: Optional[Mapping[str, Any]],
        budget_seconds: float,
    ) -> ToolResult:
        """Run one invocation in a worker thread bounded by the budget."""
        outcome: dict[str, Any] = {}
        done = threading.Event()

        def runner() -> None:
            try:
                outcome["result"] = registered.invoke(arguments)
            except BaseException as exc:  # re-raised in the caller thread
                outcome["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(
            target=runner,
            name=f"tool-execution-{registered.name}",
            daemon=True,
        )
        worker.start()
        if not done.wait(budget_seconds):
            # The invocation may still be running: its side effects are
            # unknown, not its result. The worker is detached (daemon)
            # and its eventual outcome is discarded here.
            logger.warning(
                "Tool execution exceeded its bounded budget",
                extra={
                    "extra_fields": {
                        "tool_name": registered.name,
                        "timeout_seconds": budget_seconds,
                    }
                },
            )
            return ToolResult.failure_result(
                tool_name=registered.name,
                error_code=ERROR_EXECUTION_TIMEOUT,
                message="The tool did not finish within its execution budget.",
            )
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]


default_tool_executor = ToolExecutor(
    tool_registry,
    timeout_seconds=settings.tool_timeout_seconds,
)


def execute_authorized_tool(
    tool_name: str,
    arguments: Optional[Mapping[str, Any]] = None,
    registry: Optional[ToolRegistry] = None,
) -> ToolResult:
    """Compatibility function for the internal authorized execution boundary."""
    if registry is None:
        executor = default_tool_executor
    else:
        executor = ToolExecutor(registry)
    return executor.execute(tool_name, arguments)


__all__ = [
    "ToolExecutor",
    "ToolNotImplementedError",
    "UnknownToolError",
    "execute_authorized_tool",
]
