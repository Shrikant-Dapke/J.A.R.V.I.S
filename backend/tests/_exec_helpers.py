"""Shared stubs and construction helpers for Phase 4.3 execution tests.

_NOT_ collected by pytest (module name does not match ``test_*.py``).
Every service graph built here gets an isolated in-memory (or
explicitly provided) execution-state façade: no test touches the
developer data directory or the shared singleton chain.
"""

import threading
from dataclasses import dataclass, field
from typing import ClassVar, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.policy import RiskLevel
from app.schemas.tool import ToolResult
from app.services.approval_service import ApprovalService
from app.services.approval_store import ApprovalStore
from app.services.plan_service import PlanService
from app.services.tool_base import Tool
from app.services.tool_executor import ToolExecutor
from app.services.tool_registry import ToolRegistry


OWNER_ID = "reliability-owner-hash"
SESSION_ID = "reliability-session"
OTHER_SESSION_ID = "reliability-other-session"


class _ValueInput(BaseModel):
    """Minimal bounded argument contract for the stub tools."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=16)


class ReadOnlyStubTool(Tool[_ValueInput]):
    """Policy-allowed read-only tool that always succeeds."""

    name: ClassVar[str] = "stub_ro"
    description: ClassVar[str] = "Stub read-only tool that always succeeds."
    read_only: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.READ_ONLY
    input_model: ClassVar[type[_ValueInput]] = _ValueInput

    def execute(self, arguments: _ValueInput) -> ToolResult:
        return ToolResult.success_result(
            tool_name=self.name,
            message="Read-only stub succeeded.",
            data={"value": arguments.value},
        )


class FailingReadOnlyStubTool(Tool[_ValueInput]):
    """Policy-allowed read-only tool that always fails (safe failure)."""

    name: ClassVar[str] = "stub_ro_fail"
    description: ClassVar[str] = "Stub read-only tool that always fails."
    read_only: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.READ_ONLY
    input_model: ClassVar[type[_ValueInput]] = _ValueInput

    def execute(self, arguments: _ValueInput) -> ToolResult:
        return ToolResult.failure_result(
            tool_name=self.name,
            error_code="stub_failure",
            message="Read-only stub failed deterministically.",
        )


class InstantMutatingTool(Tool[_ValueInput]):
    """Approval-required tool that succeeds immediately once executed."""

    name: ClassVar[str] = "stub_mut_ok"
    description: ClassVar[str] = "Stub mutating tool that always succeeds."
    read_only: ClassVar[bool] = False
    requires_approval: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW_RISK
    input_model: ClassVar[type[_ValueInput]] = _ValueInput

    def execute(self, arguments: _ValueInput) -> ToolResult:
        return ToolResult.success_result(
            tool_name=self.name,
            message="Mutating stub applied its effect.",
            data={"value": arguments.value},
        )


class FailingMutatingTool(Tool[_ValueInput]):
    """Approval-required tool that deterministically fails."""

    name: ClassVar[str] = "stub_mut_fail"
    description: ClassVar[str] = "Stub mutating tool that always fails."
    read_only: ClassVar[bool] = False
    requires_approval: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW_RISK
    input_model: ClassVar[type[_ValueInput]] = _ValueInput

    def execute(self, arguments: _ValueInput) -> ToolResult:
        return ToolResult.failure_result(
            tool_name=self.name,
            error_code="stub_mut_failure",
            message="Mutating stub failed deterministically.",
        )


class HangingMutatingTool(Tool[_ValueInput]):
    """Approval-required tool whose execution blocks until released.

    The class-level release event lets tests decide deterministically
    whether the execution completes (after the executor's tiny budget)
    or stays hung forever, which is what a crashed/lost run looks like.
    """

    name: ClassVar[str] = "stub_mut_hang"
    description: ClassVar[str] = "Stub mutating tool that hangs until released."
    read_only: ClassVar[bool] = False
    requires_approval: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW_RISK
    input_model: ClassVar[type[_ValueInput]] = _ValueInput

    release: ClassVar[Optional[threading.Event]] = threading.Event()

    def execute(self, arguments: _ValueInput) -> ToolResult:
        cls = type(self)
        cls.release.wait(timeout=30)
        return ToolResult.failure_result(
            tool_name=self.name,
            error_code="stub_released_after_hang",
            message="Hanging stub finished after its release event.",
        )

    @classmethod
    def rearm(cls) -> None:
        """Fresh, never-set event for the next test."""
        cls.release = threading.Event()


class SlowReadOnlyTool(Tool[_ValueInput]):
    """Read-only tool that hangs on release; proves read-only timeouts
    resolve FAILED rather than RECOVERY_REQUIRED."""

    name: ClassVar[str] = "stub_ro_hang"
    description: ClassVar[str] = "Stub read-only tool that hangs until released."
    read_only: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.READ_ONLY
    input_model: ClassVar[type[_ValueInput]] = _ValueInput

    release: ClassVar[Optional[threading.Event]] = threading.Event()

    def execute(self, arguments: _ValueInput) -> ToolResult:
        cls = type(self)
        cls.release.wait(timeout=30)
        return ToolResult.success_result(
            tool_name=self.name,
            message="Slow read-only stub finished.",
        )

    @classmethod
    def rearm(cls) -> None:
        cls.release = threading.Event()


STUB_MUTATING_NAMES = ("stub_mut_ok", "stub_mut_fail", "stub_mut_hang")


def build_registry(with_hanging: bool = True) -> ToolRegistry:
    """A fresh registry pre-populated with the deterministic stub tools."""
    registry = ToolRegistry()
    registry.register(ReadOnlyStubTool())
    registry.register(FailingReadOnlyStubTool())
    registry.register(InstantMutatingTool())
    registry.register(FailingMutatingTool())
    if with_hanging:
        HangingMutatingTool.rearm()
        SlowReadOnlyTool.rearm()
        registry.register(HangingMutatingTool())
        registry.register(SlowReadOnlyTool())
    return registry


@dataclass
class ReliabilityGraph:
    """One isolated object graph sharing exactly one state façade."""

    registry: ToolRegistry
    store: ApprovalStore
    executor: ToolExecutor
    approvals: ApprovalService
    plans: PlanService

    @property
    def db(self):
        return self.approvals.execution_db


def make_graph(
    *,
    timeout_seconds: Optional[float] = None,
    clock=None,
    state_db=None,
    with_hanging: bool = True,
) -> ReliabilityGraph:
    """Build a fully isolated approvals+plans graph over one façade."""
    registry = build_registry(with_hanging=with_hanging)
    store = ApprovalStore(clock=clock, state_db=state_db)
    executor = ToolExecutor(registry, timeout_seconds=timeout_seconds)
    approvals = ApprovalService(registry=registry, executor=executor, store=store)
    plans = PlanService(registry=registry, approvals=approvals)
    return ReliabilityGraph(
        registry=registry,
        store=store,
        executor=executor,
        approvals=approvals,
        plans=plans,
    )


_TEARDOWN_EVENTS = (HangingMutatingTool, SlowReadOnlyTool)


def release_hanging_tools() -> None:
    """Free every stub worker thread; call from fixture teardowns."""
    for cls in _TEARDOWN_EVENTS:
        cls.release.set()
