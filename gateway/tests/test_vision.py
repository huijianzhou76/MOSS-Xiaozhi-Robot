from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from moss_gateway.app import create_app
from moss_gateway.config import GatewaySettings
from moss_gateway.vision import HttpVisionProvider, VisionConfig


JPEG = b"\xff\xd8\xff\xe0moss-test-jpeg\xff\xd9"


def vision_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://vision.local/explain")
        assert request.headers.get("authorization") == "Bearer provider-secret"
        content_type = request.headers.get("content-type", "")
        assert content_type.startswith("multipart/form-data; boundary=")
        body = request.content
        assert b'name="question"' in body
        assert b"What is visible?" in body
        assert b'filename="camera.jpg"' in body
        assert JPEG in body
        return httpx.Response(
            200,
            json={"success": True, "result": "A red cup is visible."},
        )

    return httpx.MockTransport(handler)


def settings(*, provider: bool = True, max_bytes: int = 2_000_000) -> GatewaySettings:
    return GatewaySettings(
        device_token="device-secret",
        admin_token="admin-secret",
        vision_provider_url="http://vision.local/explain" if provider else "",
        vision_provider_token="provider-secret" if provider else "",
        vision_max_image_bytes=max_bytes,
    )


def test_vision_endpoint_requires_device_auth() -> None:
    app = create_app(settings(), vision_transport=vision_transport())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/vision/explain",
            data={"question": "What is visible?"},
            files={"file": ("camera.jpg", JPEG, "image/jpeg")},
        )
        assert response.status_code == 401


def test_vision_endpoint_relays_jpeg_to_configured_provider() -> None:
    app = create_app(settings(), vision_transport=vision_transport())
    headers = {"Authorization": "Bearer device-secret"}

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/vision/explain",
            headers=headers,
            data={"question": "What is visible?"},
            files={"file": ("camera.jpg", JPEG, "image/jpeg")},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["success"] is True
        assert payload["result"] == "A red cup is visible."
        assert payload["provider"] == "http"
        assert payload["image_bytes"] == len(JPEG)
        assert "device-secret" not in response.text
        assert "provider-secret" not in response.text


def test_vision_endpoint_is_unavailable_without_provider() -> None:
    app = create_app(settings(provider=False))
    headers = {"Authorization": "Bearer device-secret"}

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/vision/explain",
            headers=headers,
            data={"question": "What is visible?"},
            files={"file": ("camera.jpg", JPEG, "image/jpeg")},
        )
        assert response.status_code == 503


def test_vision_endpoint_rejects_non_jpeg_and_oversize_images() -> None:
    app = create_app(settings(max_bytes=16), vision_transport=vision_transport())
    headers = {"Authorization": "Bearer device-secret"}

    with TestClient(app) as client:
        wrong_type = client.post(
            "/api/v1/vision/explain",
            headers=headers,
            data={"question": "What is visible?"},
            files={"file": ("camera.png", b"not-a-png", "image/png")},
        )
        assert wrong_type.status_code == 415

        too_large = client.post(
            "/api/v1/vision/explain",
            headers=headers,
            data={"question": "What is visible?"},
            files={"file": ("camera.jpg", JPEG + b"x" * 64, "image/jpeg")},
        )
        assert too_large.status_code == 413


def test_health_reports_vision_configuration_without_provider_details() -> None:
    app = create_app(settings(), vision_transport=vision_transport())
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        vision = response.json()["integrations"]["vision"]
        assert vision["configured"] is True
        assert vision["provider_auth_configured"] is True
        assert vision["image_persistence"] == "disabled"
        assert "vision.local" not in response.text
        assert "provider-secret" not in response.text


def test_provider_url_rejects_embedded_credentials_and_query() -> None:
    for bad_url in (
        "http://user:pass@vision.local/explain",
        "https://vision.local/explain?key=secret",
    ):
        try:
            HttpVisionProvider(VisionConfig(provider_url=bad_url))
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe provider URL must be rejected: {bad_url}")
