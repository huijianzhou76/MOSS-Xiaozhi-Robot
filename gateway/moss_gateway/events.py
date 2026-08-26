from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import re
from threading import RLock
from typing import Any


_SENSITIVE_KEY = re.compile(
    r"(authorization|password|passwd|secret|token|api[_-]?key|ssid|mac|uuid|"
    r"last_user_text|last_assistant_text)",
    re.IGNORECASE,
)


def sanitize_payload(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON-safe, bounded, privacy-reduced event payload."""
    if depth > 6:
        return "<max-depth>"
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)[:120]
            if _SENSITIVE_KEY.search(key_text):
                clean[key_text] = "<redacted>"
            else:
                clean[key_text] = sanitize_payload(item, depth=depth + 1)
        return clean
    if isinstance(value, list):
        return [sanitize_payload(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return value[:2048]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2048]


class EventBus:
    def __init__(self, max_events: int = 1000) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._lock = RLock()
        self._sequence = 0

    def publish(self, device_id: str, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            record = {
                "seq": self._sequence,
                "time": datetime.now(timezone.utc).isoformat(),
                "device_id": device_id,
                "event": event,
                "payload": sanitize_payload(payload),
            }
            self._events.append(record)
            return dict(record)

    def list(self, *, since_seq: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 500))
        with self._lock:
            items = [item for item in self._events if item["seq"] > since_seq]
            return [dict(item) for item in items[-bounded_limit:]]

    @property
    def latest_sequence(self) -> int:
        with self._lock:
            return self._sequence
