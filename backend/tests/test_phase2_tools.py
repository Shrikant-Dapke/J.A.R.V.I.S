"""Focused Phase 2 tests for controlled tools and execution boundaries."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import BaseModel

from app.schemas.tool import ToolResult
from app.services import create_directory_tool, open_application_tool
from app.services.create_directory_tool import CreateDirectoryTool
from app.services.open_application_tool import OpenApplicationTool
from app.services.system_info_tool import SYSTEM_INFO_TOOL
from app.services.tool_base import Tool
from app.services.tool_executor import ToolExecutor
from app.services.tool_registry import ToolRegistry, UnknownToolError


class DemoInput(BaseModel):
    value: str


class DemoTool(Tool[DemoInput]):
    name = "demo_tool"
    description = "A test-only tool."
    read_only = True
    requires_approval = False
    input_model = DemoInput

    def execute(self, arguments: DemoInput) -> ToolResult:
        return ToolResult.success_result(
            tool_name=self.name,
            message="Demo completed.",
            data={"value": arguments.value},
        )


def test_registry_register_retrieve_list_and_reject_duplicate():
    registry = ToolRegistry()
    tool = DemoTool()

    registry.register(tool)

    assert registry.get("demo_tool") is tool
    assert registry.list_tools() == [tool]
    with pytest.raises(ValueError, match="already registered"):
        registry.register(tool)


def test_registry_rejects_unknown_tool():
    registry = ToolRegistry()

    assert registry.get("missing") is None
    with pytest.raises(UnknownToolError):
        registry.require("missing")


def test_tool_executor_is_registry_driven():
    registry = ToolRegistry()
    registry.register(DemoTool())
    executor = ToolExecutor(registry)

    result = executor.execute("demo_tool", {"value": "typed"})

    assert result.success is True
    assert result.data == {"value": "typed"}


def test_system_info_has_expected_safe_structure():
    result = SYSTEM_INFO_TOOL.invoke()

    assert result.success is True
    assert set(result.data or {}) == {
        "operating_system",
        "os_version",
        "hostname",
        "cpu",
        "cpu_count",
        "ram",
        "python_version",
    }
    assert result.error is None


def test_open_application_allows_registered_alias_without_running_process(
    monkeypatch,
):
    popen = Mock()
    monkeypatch.setattr(open_application_tool, "WINDOWS_PLATFORM", True)
    monkeypatch.setattr(open_application_tool.subprocess, "Popen", popen)

    result = OpenApplicationTool().invoke({"app_id": "notepad"})

    assert result.success is True
    popen.assert_called_once_with(["notepad.exe"], shell=False)


def test_open_application_rejects_unknown_alias(monkeypatch):
    popen = Mock()
    monkeypatch.setattr(open_application_tool, "WINDOWS_PLATFORM", True)
    monkeypatch.setattr(open_application_tool.subprocess, "Popen", popen)

    result = OpenApplicationTool().invoke({"app_id": "paint"})

    assert result.success is False
    assert result.error_code == "unknown_application"
    popen.assert_not_called()


def test_open_application_rejects_command_injection(monkeypatch):
    popen = Mock()
    monkeypatch.setattr(open_application_tool, "WINDOWS_PLATFORM", True)
    monkeypatch.setattr(open_application_tool.subprocess, "Popen", popen)

    result = OpenApplicationTool().invoke({"app_id": "notepad & whoami"})

    assert result.success is False
    assert result.error_code == "invalid_arguments"
    popen.assert_not_called()


def test_open_application_handles_unavailable_application(monkeypatch):
    popen = Mock(side_effect=FileNotFoundError)
    monkeypatch.setattr(open_application_tool, "WINDOWS_PLATFORM", True)
    monkeypatch.setattr(open_application_tool.subprocess, "Popen", popen)

    result = OpenApplicationTool().invoke({"app_id": "notepad"})

    assert result.success is False
    assert result.error_code == "application_unavailable"
    assert "FileNotFoundError" not in (result.error or "")


def test_create_directory_creates_only_under_approved_root(tmp_path: Path):
    tool = CreateDirectoryTool(tmp_path)

    result = tool.invoke({"path": "reports"})

    assert result.success is True
    assert result.status == "success"
    assert (tmp_path / "reports").is_dir()


def test_create_directory_existing_directory_is_distinct(tmp_path: Path):
    (tmp_path / "reports").mkdir()
    tool = CreateDirectoryTool(tmp_path)

    result = tool.invoke({"path": "reports"})

    assert result.success is True
    assert result.status == "already_exists"


@pytest.mark.parametrize("unsafe_path", ["../outside", "C:\\outside", "/outside"])
def test_create_directory_rejects_unsafe_paths(tmp_path: Path, unsafe_path: str):
    tool = CreateDirectoryTool(tmp_path)

    result = tool.invoke({"path": unsafe_path})

    assert result.success is False
    assert result.error_code == "unsafe_path"
    assert not (tmp_path.parent / "outside").exists()


def test_create_directory_handles_permission_error(tmp_path: Path, monkeypatch):
    tool = CreateDirectoryTool(tmp_path)

    def deny_creation(self, *args, **kwargs):
        raise PermissionError("test permission failure")

    monkeypatch.setattr(Path, "mkdir", deny_creation)
    result = tool.invoke({"path": "reports"})

    assert result.success is False
    assert result.error_code == "permission_denied"
    assert "test permission failure" not in (result.error or "")


def test_builtin_tools_require_future_policy_approval():
    assert create_directory_tool.CREATE_DIRECTORY_TOOL.requires_approval is True
    assert open_application_tool.OPEN_APPLICATION_TOOL.requires_approval is True
