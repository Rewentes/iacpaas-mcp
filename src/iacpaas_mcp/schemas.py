"""Явные схемы MCP: успешный ответ отделён от ошибки выполнения."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

Limit = Annotated[int, Field(ge=1, le=100)]
Depth = Annotated[int, Field(ge=0, le=5)]
NodeLimit = Annotated[int, Field(ge=1, le=2000)]
Path = Annotated[str, Field(min_length=1, max_length=1024)]
Text = Annotated[str, Field(max_length=4096)]


class ToolResponse(BaseModel):
    ok: Literal[True] = True
    data: dict[str, Any]
    backend: Literal["mock"] = "mock"
    complete: bool
    warnings: list[str] = Field(default_factory=list)
