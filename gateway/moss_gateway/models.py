from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DeviceHello(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event: Literal["hello"]
    protocol: Literal["moss-agent/1.0"]
    device_id: str | None = Field(default=None, min_length=1, max_length=80)
    backend: str = Field(default="xiaozhi", max_length=40)
    board_type: str = Field(default="unknown", max_length=80)
    board_name: str = Field(default="unknown", max_length=120)
    session_id: str = Field(default="", max_length=160)
    capabilities: dict[str, Any] = Field(default_factory=dict)

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:")
        if any(char not in allowed for char in value):
            raise ValueError("device_id contains unsupported characters")
        return value


class ToolCallRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)


class JsonRpcRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    jsonrpc: Literal["2.0"]
    id: int | str | None = None
    method: str = Field(min_length=1, max_length=100)
    params: dict[str, Any] = Field(default_factory=dict)
