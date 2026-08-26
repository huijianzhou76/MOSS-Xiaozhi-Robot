from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from .tools import ToolRegistry


_ALLOWED_DOMAINS = {"light", "switch", "fan", "climate", "cover", "scene"}
_ALLOWED_SERVICES: dict[str, set[str]] = {
    "light": {"turn_on", "turn_off"},
    "switch": {"turn_on", "turn_off"},
    "fan": {"turn_on", "turn_off", "set_percentage"},
    "climate": {"set_temperature"},
    "cover": {"open_cover", "close_cover", "stop_cover"},
    "scene": {"turn_on"},
}
_SAFE_ATTRIBUTES = {
    "friendly_name",
    "device_class",
    "unit_of_measurement",
    "brightness",
    "color_temp_kelvin",
    "percentage",
    "temperature",
    "current_temperature",
    "hvac_mode",
    "current_position",
}


class HomeAssistantError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HomeAssistantConfig:
    base_url: str
    token: str
    timeout_seconds: int = 5
    verify_tls: bool = True
    entity_allowlist: tuple[str, ...] = ()


class HomeAssistantClient:
    """Small, allowlisted Home Assistant REST adapter.

    Reads are limited to a fixed set of benign home domains and return only a
    privacy-reduced state summary. Writes require the exact entity_id to be in
    the operator-configured allowlist; there is no generic domain/service proxy.
    """

    def __init__(
        self,
        config: HomeAssistantConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport
        self._allowlist = frozenset(item.strip() for item in config.entity_allowlist if item.strip())
        self._base_url = self._validate_base_url(config.base_url) if config.base_url else ""

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self.config.token)

    @property
    def allowlist_count(self) -> int:
        return len(self._allowlist)

    def configuration_summary(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "base_url": self._base_url,
            "token_configured": bool(self.config.token),
            "token_exposed": False,
            "verify_tls": self.config.verify_tls,
            "allowed_domains": sorted(_ALLOWED_DOMAINS),
            "control_allowlist_count": len(self._allowlist),
            "default_control_policy": "deny",
        }

    async def status(self) -> dict[str, Any]:
        if not self.configured:
            return {
                **self.configuration_summary(),
                "reachable": False,
                "reason": "home_assistant_not_configured",
            }
        payload = await self._request("GET", "/api/")
        return {
            **self.configuration_summary(),
            "reachable": True,
            "api_message": str(payload.get("message", "ok"))[:120] if isinstance(payload, dict) else "ok",
        }

    async def list_entities(self, domain: str | None = None, limit: int = 200) -> dict[str, Any]:
        self._ensure_configured()
        if domain is not None:
            self._validate_domain(domain)
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")

        payload = await self._request("GET", "/api/states")
        if not isinstance(payload, list):
            raise HomeAssistantError("Home Assistant returned an invalid states response")

        entities: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("entity_id", ""))
            entity_domain = self._entity_domain(entity_id)
            if entity_domain not in _ALLOWED_DOMAINS:
                continue
            if domain and entity_domain != domain:
                continue
            entities.append(self._summarize_state(item))
            if len(entities) >= limit:
                break

        return {"entities": entities, "count": len(entities), "domain": domain}

    async def get_entity(self, entity_id: str) -> dict[str, Any]:
        self._validate_entity_id(entity_id)
        payload = await self._request("GET", f"/api/states/{entity_id}")
        if not isinstance(payload, dict):
            raise HomeAssistantError("Home Assistant returned an invalid entity response")
        return self._summarize_state(payload)

    async def call_entity_service(
        self,
        entity_id: str,
        service: str,
        service_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        domain = self._validate_entity_id(entity_id)
        if entity_id not in self._allowlist:
            raise PermissionError(
                f"Home Assistant control denied: {entity_id} is not in MOSS_HA_ENTITY_ALLOWLIST"
            )
        if service not in _ALLOWED_SERVICES[domain]:
            raise PermissionError(f"Home Assistant service not allowed: {domain}.{service}")

        before = await self.get_entity(entity_id)
        body: dict[str, Any] = {"entity_id": entity_id}
        if service_data:
            body.update(service_data)

        changed = await self._request("POST", f"/api/services/{domain}/{service}", json=body)
        after = await self.get_entity(entity_id)
        return {
            "ok": True,
            "domain": domain,
            "service": service,
            "entity_id": entity_id,
            "before": before,
            "after": after,
            "changed_states": len(changed) if isinstance(changed, list) else None,
        }

    async def light_turn_on(self, entity_id: str) -> dict[str, Any]:
        self._require_domain(entity_id, "light")
        return await self.call_entity_service(entity_id, "turn_on")

    async def light_turn_off(self, entity_id: str) -> dict[str, Any]:
        self._require_domain(entity_id, "light")
        return await self.call_entity_service(entity_id, "turn_off")

    async def light_set_brightness(self, entity_id: str, brightness_pct: int) -> dict[str, Any]:
        self._require_domain(entity_id, "light")
        if brightness_pct < 0 or brightness_pct > 100:
            raise ValueError("brightness_pct must be between 0 and 100")
        return await self.call_entity_service(
            entity_id,
            "turn_on",
            {"brightness_pct": brightness_pct},
        )

    async def switch_turn_on(self, entity_id: str) -> dict[str, Any]:
        self._require_domain(entity_id, "switch")
        return await self.call_entity_service(entity_id, "turn_on")

    async def switch_turn_off(self, entity_id: str) -> dict[str, Any]:
        self._require_domain(entity_id, "switch")
        return await self.call_entity_service(entity_id, "turn_off")

    async def fan_turn_on(self, entity_id: str) -> dict[str, Any]:
        self._require_domain(entity_id, "fan")
        return await self.call_entity_service(entity_id, "turn_on")

    async def fan_turn_off(self, entity_id: str) -> dict[str, Any]:
        self._require_domain(entity_id, "fan")
        return await self.call_entity_service(entity_id, "turn_off")

    async def fan_set_percentage(self, entity_id: str, percentage: int) -> dict[str, Any]:
        self._require_domain(entity_id, "fan")
        if percentage < 0 or percentage > 100:
            raise ValueError("percentage must be between 0 and 100")
        return await self.call_entity_service(entity_id, "set_percentage", {"percentage": percentage})

    async def climate_set_temperature(self, entity_id: str, temperature: float) -> dict[str, Any]:
        self._require_domain(entity_id, "climate")
        if temperature < 5 or temperature > 35:
            raise ValueError("temperature must be between 5 and 35 Celsius")
        return await self.call_entity_service(entity_id, "set_temperature", {"temperature": temperature})

    async def cover_open(self, entity_id: str) -> dict[str, Any]:
        self._require_domain(entity_id, "cover")
        return await self.call_entity_service(entity_id, "open_cover")

    async def cover_close(self, entity_id: str) -> dict[str, Any]:
        self._require_domain(entity_id, "cover")
        return await self.call_entity_service(entity_id, "close_cover")

    async def cover_stop(self, entity_id: str) -> dict[str, Any]:
        self._require_domain(entity_id, "cover")
        return await self.call_entity_service(entity_id, "stop_cover")

    async def scene_activate(self, entity_id: str) -> dict[str, Any]:
        self._require_domain(entity_id, "scene")
        return await self.call_entity_service(entity_id, "turn_on")

    async def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> Any:
        self._ensure_configured()
        headers = {
            "Authorization": f"Bearer {self.config.token}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(float(self.config.timeout_seconds))
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=timeout,
                verify=self.config.verify_tls,
                transport=self._transport,
            ) as client:
                response = await client.request(method, path, json=json)
        except httpx.RequestError as exc:
            raise HomeAssistantError(f"Home Assistant request failed: {exc.__class__.__name__}") from exc

        if response.status_code == 404:
            raise HomeAssistantError("Home Assistant entity or API endpoint not found")
        if response.status_code in {401, 403}:
            raise HomeAssistantError("Home Assistant authorization failed")
        if response.status_code >= 400:
            raise HomeAssistantError(f"Home Assistant returned HTTP {response.status_code}")

        try:
            return response.json()
        except ValueError as exc:
            raise HomeAssistantError("Home Assistant returned invalid JSON") from exc

    def _summarize_state(self, item: dict[str, Any]) -> dict[str, Any]:
        entity_id = str(item.get("entity_id", ""))
        domain = self._validate_entity_id(entity_id)
        attributes = item.get("attributes")
        safe_attributes: dict[str, Any] = {}
        if isinstance(attributes, dict):
            for key in _SAFE_ATTRIBUTES:
                if key in attributes and isinstance(attributes[key], (str, int, float, bool, type(None))):
                    safe_attributes[key] = attributes[key]

        return {
            "entity_id": entity_id,
            "domain": domain,
            "state": str(item.get("state", "unknown"))[:120],
            "attributes": safe_attributes,
            "controllable": entity_id in self._allowlist,
        }

    def _validate_entity_id(self, entity_id: str) -> str:
        if not entity_id or len(entity_id) > 160 or entity_id.count(".") != 1:
            raise ValueError("invalid Home Assistant entity_id")
        domain, object_id = entity_id.split(".", 1)
        self._validate_domain(domain)
        if not object_id or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_" for ch in object_id):
            raise ValueError("invalid Home Assistant entity_id")
        return domain

    def _require_domain(self, entity_id: str, expected: str) -> None:
        actual = self._validate_entity_id(entity_id)
        if actual != expected:
            raise ValueError(f"entity_id must be in {expected} domain")

    @staticmethod
    def _entity_domain(entity_id: str) -> str:
        return entity_id.split(".", 1)[0] if "." in entity_id else ""

    @staticmethod
    def _validate_domain(domain: str) -> None:
        if domain not in _ALLOWED_DOMAINS:
            raise ValueError(f"unsupported Home Assistant domain: {domain}")

    def _ensure_configured(self) -> None:
        if not self.configured:
            raise HomeAssistantError("Home Assistant is not configured")

    @staticmethod
    def _validate_base_url(value: str) -> str:
        raw = value.strip().rstrip("/")
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("MOSS_HA_URL must be an http:// or https:// origin")
        if parsed.username or parsed.password:
            raise ValueError("MOSS_HA_URL must not contain embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("MOSS_HA_URL must not contain query or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("MOSS_HA_URL must point to the Home Assistant origin, not a subpath")
        return raw


def register_home_assistant_tools(registry: ToolRegistry, client: HomeAssistantClient) -> None:
    entity_schema = {
        "type": "object",
        "properties": {"entity_id": {"type": "string"}},
        "required": ["entity_id"],
        "additionalProperties": False,
    }

    registry.register(
        name="home.status",
        description="Read Home Assistant integration configuration and API reachability without exposing the token.",
        risk="read_only",
        handler=lambda _: client.status(),
    )
    registry.register(
        name="home.entities.list",
        description="List privacy-reduced Home Assistant entities from approved domains. Reads do not grant control permission.",
        risk="read_only",
        input_schema={
            "type": "object",
            "properties": {
                "domain": {"type": ["string", "null"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "additionalProperties": False,
        },
        handler=lambda args: client.list_entities(args.get("domain"), int(args.get("limit", 200))),
    )
    registry.register(
        name="home.entity.get",
        description="Read one privacy-reduced Home Assistant entity state.",
        risk="read_only",
        input_schema=entity_schema,
        handler=lambda args: client.get_entity(str(args["entity_id"])),
    )

    def action(name: str, description: str, handler: Any, schema: dict[str, Any] = entity_schema) -> None:
        registry.register(
            name=name,
            description=description + " The entity must be explicitly allowlisted by the gateway operator.",
            risk="physical",
            input_schema=schema,
            handler=handler,
        )

    action("home.light.turn_on", "Turn on an allowlisted Home Assistant light.", lambda a: client.light_turn_on(str(a["entity_id"])))
    action("home.light.turn_off", "Turn off an allowlisted Home Assistant light.", lambda a: client.light_turn_off(str(a["entity_id"])))
    action(
        "home.light.set_brightness",
        "Set brightness percentage for an allowlisted Home Assistant light.",
        lambda a: client.light_set_brightness(str(a["entity_id"]), int(a["brightness_pct"])),
        {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "brightness_pct": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            "required": ["entity_id", "brightness_pct"],
            "additionalProperties": False,
        },
    )
    action("home.switch.turn_on", "Turn on an allowlisted Home Assistant switch.", lambda a: client.switch_turn_on(str(a["entity_id"])))
    action("home.switch.turn_off", "Turn off an allowlisted Home Assistant switch.", lambda a: client.switch_turn_off(str(a["entity_id"])))
    action("home.fan.turn_on", "Turn on an allowlisted Home Assistant fan.", lambda a: client.fan_turn_on(str(a["entity_id"])))
    action("home.fan.turn_off", "Turn off an allowlisted Home Assistant fan.", lambda a: client.fan_turn_off(str(a["entity_id"])))
    action(
        "home.fan.set_percentage",
        "Set percentage for an allowlisted Home Assistant fan.",
        lambda a: client.fan_set_percentage(str(a["entity_id"]), int(a["percentage"])),
        {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "percentage": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            "required": ["entity_id", "percentage"],
            "additionalProperties": False,
        },
    )
    action(
        "home.climate.set_temperature",
        "Set target temperature for an allowlisted Home Assistant climate entity.",
        lambda a: client.climate_set_temperature(str(a["entity_id"]), float(a["temperature"])),
        {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "temperature": {"type": "number", "minimum": 5, "maximum": 35},
            },
            "required": ["entity_id", "temperature"],
            "additionalProperties": False,
        },
    )
    action("home.cover.open", "Open an allowlisted Home Assistant cover.", lambda a: client.cover_open(str(a["entity_id"])))
    action("home.cover.close", "Close an allowlisted Home Assistant cover.", lambda a: client.cover_close(str(a["entity_id"])))
    action("home.cover.stop", "Stop an allowlisted Home Assistant cover.", lambda a: client.cover_stop(str(a["entity_id"])))
    action("home.scene.activate", "Activate an allowlisted Home Assistant scene.", lambda a: client.scene_activate(str(a["entity_id"])))
