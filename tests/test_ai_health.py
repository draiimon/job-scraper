import time

from src.ai import GeminiManager, KeyState
from src.config import Settings


def test_ai_health_discloses_rate_limited_without_exposing_key_material():
    manager = GeminiManager(Settings(ai_enabled=True, cover_letter_mode="ai"))
    manager.keys = [KeyState("1", "only-for-test-A3F2", "shared", cooldown_until=time.time() + 30)]

    health = manager.health()

    assert health["status"] == "RATE_LIMITED"
    assert not health["available"]
    assert health["active_cooldowns"] == 1
    assert "only-for-test" not in str(health)


def test_ai_health_discloses_disabled_mode():
    manager = GeminiManager(Settings(ai_enabled=False, cover_letter_mode="template"))
    manager.keys = [KeyState("1", "only-for-test-A3F2", "shared")]

    assert manager.health()["status"] == "DISABLED"
