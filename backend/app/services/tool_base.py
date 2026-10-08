"""Generic typed tool interface and common invocation handling."""

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Generic, Mapping, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.logging import get_logger
from app.schemas.tool import ToolDefinition, ToolResult


logger = get_logger(__name__)

InputModelT = TypeVar("InputModelT", bound=BaseModel)


class ToolExecutionError(Exception):
    """Expected tool failure with a safe public error classification."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.safe_message = message


class Tool(ABC, Generic[InputModelT]):
    """Base class for a bounded tool with typed input and output."""

    name: ClassVar[str]
    description: ClassVar[str]
    read_only: ClassVar[bool]
    requires_approval: ClassVar[bool]
    input_model: ClassVar[type[InputModelT]]

    @property
    def definition(self) -> ToolDefinition:
        """Return metadata that can be shown to policy or a future UI."""
        return ToolDefinition(
            name=self.name,
            description=self.description,
            read_only=self.read_only,
            requires_approval=self.requires_approval,
            input_schema=self.input_model.model_json_schema(),
        )

    def invoke(
        self, arguments: Mapping[str, Any] | InputModelT | None = None
    ) -> ToolResult:
        """Validate arguments and execute while containing unexpected errors."""
        try:
            parsed = (
                arguments
                if isinstance(arguments, self.input_model)
                else self.input_model.model_validate(arguments or {})
            )
        except ValidationError:
            return ToolResult.failure_result(
                tool_name=self.name,
                error_code="invalid_arguments",
                message="The tool arguments are invalid.",
            )

        try:
            result = self.execute(parsed)
            if result.tool_name != self.name:
                logger.error(
                    "Tool returned an unexpected tool name",
                    extra={
                        "extra_fields": {
                            "expected_tool": self.name,
                            "returned_tool": result.tool_name,
                        }
                    },
                )
                return ToolResult.failure_result(
                    tool_name=self.name,
                    error_code="invalid_tool_result",
                    message="The tool returned an invalid result.",
                )
            return result
        except ToolExecutionError as exc:
            logger.warning(
                "Controlled tool execution failed",
                extra={
                    "extra_fields": {
                        "tool_name": self.name,
                        "error_code": exc.error_code,
                    }
                },
            )
            return ToolResult.failure_result(
                tool_name=self.name,
                error_code=exc.error_code,
                message=exc.safe_message,
            )
        except Exception as exc:  # pragma: no cover - defensive boundary
            logger.exception(
                "Unexpected controlled tool exception",
                extra={
                    "extra_fields": {
                        "tool_name": self.name,
                        "error_type": type(exc).__name__,
                    }
                },
            )
            return ToolResult.failure_result(
                tool_name=self.name,
                error_code="tool_execution_failed",
                message="The tool could not complete the requested operation.",
            )

    @abstractmethod
    def execute(self, arguments: InputModelT) -> ToolResult:
        """Execute validated arguments. Implementations must not accept commands."""
        raise NotImplementedError
