import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.main import create_app


@pytest.mark.asyncio
async def test_database_devices_and_preferences(test_db: Database):
    # 1. Register device
    await test_db.register_device(
        user_email="alice@gmail.com",
        device_id="phone_123",
        fcm_token="token_abc",
        device_name="Pixel 8",
    )
    devices = await test_db.get_user_devices("alice@gmail.com")
    assert len(devices) == 1
    assert devices[0]["device_name"] == "Pixel 8"

    # 2. Check tokens before preferences
    tokens = await test_db.get_subscribed_fcm_tokens("frontdoor")
    assert "token_abc" in tokens

    # 3. Disable frontdoor alerts for Alice
    await test_db.set_user_camera_preference("alice@gmail.com", "frontdoor", enabled=False)
    prefs = await test_db.get_user_camera_preferences("alice@gmail.com")
    assert prefs["frontdoor"]["enabled"] is False

    tokens_disabled = await test_db.get_subscribed_fcm_tokens("frontdoor")
    assert "token_abc" not in tokens_disabled

    # Other camera is still subscribed
    tokens_backyard = await test_db.get_subscribed_fcm_tokens("backyard")
    assert "token_abc" in tokens_backyard

    # 4. Mute backyard for Alice into the future
    future_time = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    await test_db.set_user_camera_preference("alice@gmail.com", "backyard", enabled=True, mute_until=future_time)
    tokens_muted = await test_db.get_subscribed_fcm_tokens("backyard")
    assert "token_abc" not in tokens_muted

    # 5. Delete device
    deleted = await test_db.delete_device("alice@gmail.com", "phone_123")
    assert deleted is True
    assert len(await test_db.get_user_devices("alice@gmail.com")) == 0


def test_device_and_preference_api(test_settings: Settings):
    app = create_app(test_settings)
    with TestClient(app) as client:
        # Register device
        res = client.post("/devices/register", json={
            "device_id": "test_device_1",
            "fcm_token": "fcm_test_token",
            "device_name": "Galaxy S24",
        })
        assert res.status_code == 200
        assert res.json()["status"] == "registered"

        # List devices
        res = client.get("/devices")
        assert res.status_code == 200
        assert len(res.json()["devices"]) >= 1

        # Set preference
        res = client.put("/preferences", json={
            "camera_name": "frontdoor",
            "enabled": True,
        })
        assert res.status_code == 200
        assert res.json()["status"] == "updated"

        # Get preferences
        res = client.get("/preferences")
        assert res.status_code == 200
        assert "frontdoor" in res.json()["preferences"]
