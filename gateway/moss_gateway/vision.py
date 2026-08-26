from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


class VisionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VisionConfig:
    provider_url: str = ""
    provider_token: str = ""
    timeout_seconds: int = 30
    verify_tls: bool = True
    max_image_bytes: int = 2_000_000


class HttpVisionProvider:
    """Reviewed HTTP adapter for a Host/RDK-side vision inference service.

    The upstream service uses the same compact multipart contract as the ESP32
    camera: `question` plus a JPEG `file`. This keeps model/vendor details out
    of the MCU and lets the Host/RDK later point at a local VLM service or a
    separately reviewed cloud adapter.
    """

    def __init__(
        self,
        config: VisionConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport
        self._provider_url = (
            self._validate_provider_url(config.provider_url)
            if config.provider_url
            else ""
        )

    @property
    def configured(self) -> bool:
        return bool(self._provider_url)

    def configuration_summary(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "provider_auth_configured": bool(self.config.provider_token),
            "verify_tls": self.config.verify_tls,
            "timeout_seconds": self.config.timeout_seconds,
            "max_image_bytes": self.config.max_image_bytes,
            "image_persistence": "disabled",
        }

    async def explain(
        self,
        *,
        question: str,
        image: bytes,
        content_type: str = "image/jpeg",
    ) -> dict[str, Any]:
        if not self.configured:
            raise VisionError("MOSS vision provider is not configured")
        question = question.strip()
        if not question or len(question) > 1000:
            raise ValueError("vision question must be 1-1000 characters")
        if not image:
            raise ValueError("vision image is empty")
        if len(image) > self.config.max_image_bytes:
            raise ValueError("vision image exceeds configured size limit")
        if content_type != "image/jpeg" or not image.startswith(b"\xff\xd8"):
            raise ValueError("vision input must be a JPEG image")

        headers: dict[str, str] = {}
        if self.config.provider_token:
            headers["Authorization"] = f"Bearer {self.config.provider_token}"

        timeout = httpx.Timeout(float(self.config.timeout_seconds))
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                verify=self.config.verify_tls,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._provider_url,
                    headers=headers,
                    data={"question": question},
                    files={"file": ("camera.jpg", image, "image/jpeg")},
                )
        except httpx.RequestError as exc:
            raise VisionError(
                f"vision provider request failed: {exc.__class__.__name__}"
            ) from exc

        if response.status_code in {401, 403}:
            raise VisionError("vision provider authorization failed")
        if response.status_code >= 400:
            raise VisionError(
                f"vision provider returned HTTP {response.status_code}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise VisionError("vision provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise VisionError("vision provider returned an invalid response object")
        if payload.get("success") is False:
            message = str(payload.get("message", "vision provider failed"))[:300]
            raise VisionError(message)

        answer: str | None = None
        for key in ("result", "answer", "text"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                answer = value.strip()
                break
        if not answer:
            raise VisionError("vision provider response has no result text")

        return {
            "success": True,
            "result": answer[:16000],
            "provider": "http",
            "image_bytes": len(image),
        }

    @staticmethod
    def _validate_provider_url(value: str) -> str:
        raw = value.strip()
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(
                "MOSS_VISION_PROVIDER_URL must be an http:// or https:// URL"
            )
        if parsed.username or parsed.password:
            raise ValueError(
                "MOSS_VISION_PROVIDER_URL must not contain embedded credentials"
            )
        if parsed.fragment or parsed.query:
            raise ValueError(
                "MOSS_VISION_PROVIDER_URL must not contain query or fragment"
            )
        return raw
