from __future__ import annotations

from dataclasses import dataclass
import os
import secrets


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = int(raw)
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    device_token: str = ""
    admin_token: str = ""
    allow_insecure: bool = False
    max_message_bytes: int = 65536
    hello_timeout_seconds: int = 10
    event_buffer_size: int = 1000

    @classmethod
    def from_env(cls) -> "GatewaySettings":
        return cls(
            device_token=os.getenv("MOSS_GATEWAY_DEVICE_TOKEN", "").strip(),
            admin_token=os.getenv("MOSS_GATEWAY_ADMIN_TOKEN", "").strip(),
            allow_insecure=_env_bool("MOSS_GATEWAY_ALLOW_INSECURE", False),
            max_message_bytes=_env_int(
                "MOSS_GATEWAY_MAX_MESSAGE_BYTES", 65536, 4096, 1048576
            ),
            hello_timeout_seconds=_env_int(
                "MOSS_GATEWAY_HELLO_TIMEOUT_SECONDS", 10, 1, 60
            ),
            event_buffer_size=_env_int(
                "MOSS_GATEWAY_EVENT_BUFFER_SIZE", 1000, 100, 10000
            ),
        )

    @property
    def device_auth_configured(self) -> bool:
        return bool(self.device_token)

    @property
    def admin_auth_configured(self) -> bool:
        return bool(self.admin_token)

    @property
    def secure_mode(self) -> bool:
        return (
            not self.allow_insecure
            and self.device_auth_configured
            and self.admin_auth_configured
        )

    def authorize_device(self, presented: str | None) -> bool:
        if self.device_token:
            return bool(presented) and secrets.compare_digest(
                presented, self.device_token
            )
        return self.allow_insecure

    def authorize_admin(self, presented: str | None) -> bool:
        if self.admin_token:
            return bool(presented) and secrets.compare_digest(
                presented, self.admin_token
            )
        return self.allow_insecure
