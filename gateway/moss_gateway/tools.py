from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Any, Awaitable, Callable

from .events import sanitize_payload


ToolHandler = Callable[[dict[str, Any]], Any | Awaitable[Any]]
_ALLOWED_RISKS = {
    "read_only",
    "low_impact",
    "sensitive",
    "physical",
    "destructive",
}


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    risk: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk,
            "inputSchema": self.input_schema,
        }


class ToolRegistry:
    """Strict host-side tool registry used by REST and MCP routing.

    There is intentionally no generic shell/HTTP execution tool. External
    integrations such as Home Assistant must register named, reviewed tools.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        risk: str,
        handler: ToolHandler,
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        if not name or len(name) > 120:
            raise ValueError("invalid tool name")
        if name in self._tools:
            raise ValueError(f"duplicate tool: {name}")
        if risk not in _ALLOWED_RISKS:
            raise ValueError(f"unsupported tool risk: {risk}")

        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            risk=risk,
            input_schema=input_schema or {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=handler,
        )

    def list(self) -> list[dict[str, Any]]:
        return [tool.public_dict() for tool in self._tools.values()]

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(name)

        result = tool.handler(arguments)
        if inspect.isawaitable(result):
            result = await result
        return sanitize_payload(result)

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)
