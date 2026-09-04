# ConfigProvider abstraction
from services.common.config_db import ConfigProvider
from typing import Any
import json

config = ConfigProvider()
FAST_UPDATE_WINDOW_SECONDS: float = float(config.get("FAST_UPDATE_WINDOW_SECONDS", 30))


class Settings:
    @staticmethod
    def sl_max_pips() -> float:
        return float(config.get("SL_MAX_PIPS", 120.0))

    @staticmethod
    def load() -> dict[str, Any]:
        return {
            "redis_url": config.get("REDIS_URL", "redis://redis:6379/0"),
            "log_level": config.get("LOG_LEVEL", "INFO"),
            "trading_windows": config.get("TRADING_WINDOWS", "03:00-12:00,08:00-17:00"),
            "entry_wait_seconds": int(config.get("ENTRY_WAIT_SECONDS", 60)),
            "entry_poll_ms": int(config.get("ENTRY_POLL_MS", 500)),
            "entry_buffer_points": float(config.get("ENTRY_BUFFER_POINTS", 0.0)),
            "dedup_ttl_seconds": float(config.get("DEDUP_TTL_SECONDS", 120.0)),
            "enable_notifications": config.get("ENABLE_NOTIFICATIONS", "true") in ("true", "1", "yes", "on"),
            "fast_update_window_seconds": float(config.get("FAST_UPDATE_WINDOW_SECONDS", 30)),
            "TG_API_ID": config.get("TG_API_ID"),
            "TG_API_HASH": config.get("TG_API_HASH"),
            "TG_PHONE": config.get("TG_PHONE"),
        }

    @staticmethod
    def accounts() -> list[dict]:
        return json.loads(config.get("ACCOUNTS_JSON", "[]"))

    @staticmethod
    def signal_providers() -> list[dict]:
        return []

    @staticmethod
    def channel_providers() -> dict:
        return {}
