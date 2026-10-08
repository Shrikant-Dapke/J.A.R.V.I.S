"""Bounded read-only system information tool."""

import ctypes
import os
import platform
import socket
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from app.schemas.policy import RiskLevel
from app.schemas.tool import ToolResult
from app.services.tool_base import Tool


TOOL_NAME = "get_system_info"


class GetSystemInfoInput(BaseModel):
    """This read-only tool does not accept any arguments."""

    model_config = ConfigDict(extra="forbid")


def _total_memory_gb() -> str:
    """Return total RAM using platform APIs without reading environment data."""
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return f"{status.total_physical / (1024 ** 3):.1f} GB"
        return "unavailable"

    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
        return f"{(page_size * page_count) / (1024 ** 3):.1f} GB"
    except (AttributeError, OSError, ValueError):
        return "unavailable"


class SystemInfoTool(Tool[GetSystemInfoInput]):
    """Return a documented set of non-sensitive host information."""

    name: ClassVar[str] = TOOL_NAME
    description: ClassVar[str] = (
        "Return bounded non-sensitive operating system and runtime information."
    )
    read_only: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    risk_level: ClassVar[RiskLevel] = RiskLevel.READ_ONLY
    input_model: ClassVar[type[GetSystemInfoInput]] = GetSystemInfoInput

    def execute(self, arguments: GetSystemInfoInput) -> ToolResult:
        del arguments
        processor = platform.processor() or platform.machine() or "unavailable"
        data = {
            "operating_system": platform.system() or "unavailable",
            "os_version": platform.version() or "unavailable",
            "hostname": socket.gethostname(),
            "cpu": processor,
            "cpu_count": str(os.cpu_count() or "unavailable"),
            "ram": _total_memory_gb(),
            "python_version": platform.python_version(),
        }
        return ToolResult.success_result(
            tool_name=self.name,
            message="System information retrieved.",
            data=data,
        )


SYSTEM_INFO_TOOL = SystemInfoTool()


def get_system_info() -> ToolResult:
    """Compatibility function for callers of the Phase 1 stub."""
    return SYSTEM_INFO_TOOL.invoke()


TOOL_DEFINITION = SYSTEM_INFO_TOOL.definition
