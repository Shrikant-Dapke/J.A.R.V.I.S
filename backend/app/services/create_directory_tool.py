"""Safe directory creation under one approved workspace root."""

from pathlib import Path, PurePath, PureWindowsPath
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.policy import RiskLevel
from app.schemas.tool import ToolResult
from app.services.tool_base import Tool


TOOL_NAME = "create_directory"
DEFAULT_APPROVED_ROOT = (
    Path(__file__).resolve().parents[3] / "data" / "workspace"
)


class CreateDirectoryInput(BaseModel):
    """A relative directory path; the approved root is tool configuration."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str = Field(min_length=1, max_length=240)

    @field_validator("path")
    @classmethod
    def validate_path_text(cls, value: str) -> str:
        """Reject control characters before filesystem operations."""
        if not value.strip() or "\x00" in value:
            raise ValueError("path must be a non-empty relative path")
        if any(ord(character) < 32 for character in value):
            raise ValueError("path contains an invalid control character")
        return value


class CreateDirectoryTool(Tool[CreateDirectoryInput]):
    """Create directories only beneath a configured, approved root."""

    name: ClassVar[str] = TOOL_NAME
    description: ClassVar[str] = (
        "Create a directory beneath the configured approved workspace root."
    )
    read_only: ClassVar[bool] = False
    requires_approval: ClassVar[bool] = True
    risk_level: ClassVar[RiskLevel] = RiskLevel.LOW_RISK
    input_model: ClassVar[type[CreateDirectoryInput]] = CreateDirectoryInput

    def __init__(self, approved_root: Path | str = DEFAULT_APPROVED_ROOT) -> None:
        self.approved_root = Path(approved_root)

    def _resolve_target(self, relative_path: str) -> tuple[Path, Path] | None:
        """Resolve the root and target while preventing traversal/symlink escape."""
        root = self.approved_root
        if not root.exists() or not root.is_dir():
            return None

        root_resolved = root.resolve()
        candidate = root / relative_path
        candidate_resolved = candidate.resolve(strict=False)
        try:
            candidate_resolved.relative_to(root_resolved)
        except ValueError:
            return None
        return root_resolved, candidate_resolved

    def execute(self, arguments: CreateDirectoryInput) -> ToolResult:
        relative_path = arguments.path.strip()
        path_object = PurePath(relative_path)
        windows_path = PureWindowsPath(relative_path)

        if (
            path_object.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or any(part in {"", ".", ".."} for part in path_object.parts)
            or any(part in {"", ".", ".."} for part in windows_path.parts)
        ):
            return ToolResult.failure_result(
                tool_name=self.name,
                error_code="unsafe_path",
                message="The directory path must stay inside the approved root.",
            )

        resolved = self._resolve_target(relative_path)
        if resolved is None:
            return ToolResult.failure_result(
                tool_name=self.name,
                error_code="unsafe_path",
                message="The directory path must stay inside the approved root.",
            )

        root_resolved, target = resolved
        if target == root_resolved:
            return ToolResult.failure_result(
                tool_name=self.name,
                error_code="unsafe_path",
                message="The approved root itself cannot be created.",
            )

        if target.exists():
            if target.is_dir():
                return ToolResult.already_exists_result(
                    tool_name=self.name,
                    message="The requested directory already exists.",
                    data={"path": relative_path},
                )
            return ToolResult.failure_result(
                tool_name=self.name,
                error_code="path_not_directory",
                message="A non-directory item already exists at that path.",
            )

        try:
            target.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            if target.is_dir():
                return ToolResult.already_exists_result(
                    tool_name=self.name,
                    message="The requested directory already exists.",
                    data={"path": relative_path},
                )
            return ToolResult.failure_result(
                tool_name=self.name,
                error_code="path_not_directory",
                message="A non-directory item already exists at that path.",
            )
        except PermissionError:
            return ToolResult.failure_result(
                tool_name=self.name,
                error_code="permission_denied",
                message="Permission was denied while creating the directory.",
            )
        except OSError:
            return ToolResult.failure_result(
                tool_name=self.name,
                error_code="directory_creation_failed",
                message="The directory could not be created.",
            )

        if not target.is_dir():
            return ToolResult.failure_result(
                tool_name=self.name,
                error_code="verification_failed",
                message="The directory could not be verified after creation.",
            )

        return ToolResult.success_result(
            tool_name=self.name,
            message="The directory was created.",
            data={"path": relative_path},
        )


CREATE_DIRECTORY_TOOL = CreateDirectoryTool()


def create_directory(path: str) -> ToolResult:
    """Invoke directory creation through the typed boundary."""
    return CREATE_DIRECTORY_TOOL.invoke({"path": path})


TOOL_DEFINITION = CREATE_DIRECTORY_TOOL.definition
