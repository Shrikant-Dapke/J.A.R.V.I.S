"""Strictly allowlisted Windows application launcher."""

import os
import subprocess
from pathlib import PureWindowsPath
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.schemas.policy import RiskLevel
from app.schemas.tool import ToolResult
from app.services.tool_base import Tool


TOOL_NAME = "open_application"


class OpenApplicationInput(BaseModel):
    """Structured application identifier; no command or arguments are accepted."""

    model_config = ConfigDict(extra="forbid")

    app_id: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_-]+$")


WINDOWS_PLATFORM = os.name == "nt"


def _validated_absolute_windows_path(value: str) -> str:
    """Return an absolute path without traversal or search-path resolution."""
    path = PureWindowsPath(value)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise RuntimeError("Configured application paths must be absolute and clean")
    return str(path)


_system_root = _validated_absolute_windows_path(
    os.environ.get("SystemRoot", r"C:\Windows")
)
_system_root_path = PureWindowsPath(_system_root)

# Values are fixed by the application or trusted local configuration, never
# supplied by the model or request caller. No PATH lookup is performed.
APPLICATION_ALLOWLIST: dict[str, str] = {
    "notepad": _validated_absolute_windows_path(
        str(_system_root_path / "System32" / "notepad.exe")
    ),
    "calculator": _validated_absolute_windows_path(
        str(_system_root_path / "System32" / "calc.exe")
    ),
    "explorer": _validated_absolute_windows_path(
        str(_system_root_path / "explorer.exe")
    ),
}
if settings.code_executable_path:
    APPLICATION_ALLOWLIST["code"] = _validated_absolute_windows_path(
        settings.code_executable_path
    )


class OpenApplicationTool(Tool[OpenApplicationInput]):
    """Launch one explicitly registered Windows application without a shell."""

    name: ClassVar[str] = TOOL_NAME
    description: ClassVar[str] = "Open a registered Windows application alias."
    read_only: ClassVar[bool] = False
    requires_approval: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW_RISK
    input_model: ClassVar[type[OpenApplicationInput]] = OpenApplicationInput

    def execute(self, arguments: OpenApplicationInput) -> ToolResult:
        executable = APPLICATION_ALLOWLIST.get(arguments.app_id)
        if executable is None:
            return ToolResult.failure_result(
                tool_name=self.name,
                error_code="unknown_application",
                message="The requested application is not allowlisted.",
            )

        if not WINDOWS_PLATFORM:
            return ToolResult.failure_result(
                tool_name=self.name,
                error_code="unsupported_platform",
                message="Opening applications is supported only on Windows.",
            )

        try:
            subprocess.Popen([executable], shell=False)
        except FileNotFoundError:
            return ToolResult.failure_result(
                tool_name=self.name,
                error_code="application_unavailable",
                message="The allowlisted application is not available.",
            )
        except OSError:
            return ToolResult.failure_result(
                tool_name=self.name,
                error_code="application_launch_failed",
                message="The allowlisted application could not be opened.",
            )

        return ToolResult.success_result(
            tool_name=self.name,
            message="The allowlisted application launch was requested.",
            data={"app_id": arguments.app_id},
        )


OPEN_APPLICATION_TOOL = OpenApplicationTool()


def open_application(app_id: str) -> ToolResult:
    """Invoke the launcher through its typed boundary."""
    return OPEN_APPLICATION_TOOL.invoke({"app_id": app_id})


TOOL_DEFINITION = OPEN_APPLICATION_TOOL.definition
