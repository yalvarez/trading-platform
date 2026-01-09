import os
# Configuración unificada de canales y parsers
CHANNELS_CONFIG_JSON = os.getenv("CHANNELS_CONFIG_JSON", "{}")
import os, json
from dataclasses import dataclass
from typing import Any, Optional

def env(key: str, default: Optional[str] = None) -> str:
    v = os.getenv(key, default)
    if v is None:
        raise RuntimeError(f"Missing env var: {key}")
    return v

def env_int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))

def env_float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))

def env_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")

@dataclass
class Settings:
    redis_url: str
    log_level: str
    trading_windows: str
    entry_wait_seconds: int
    entry_poll_ms: int
    entry_buffer_points: float
    accounts_json: str
    # Advanced trading settings
    dedup_ttl_seconds: float
    enable_notifications: bool
    enable_advanced_trade_mgmt: bool
    scalp_tp1_percent: float
    scalp_tp2_percent: float
    long_tp1_percent: float
    long_tp2_percent: float
    enable_breakeven: bool
    breakeven_offset_pips: float
    enable_trailing: bool
    trailing_activation_pips: float
    trailing_stop_pips: float
    enable_addon: bool
    addon_max_count: int
    addon_lot_factor: float

    @staticmethod
    def load() -> "Settings":
        return Settings(
            redis_url=env("REDIS_URL", "redis://redis:6379/0"),
            log_level=env("LOG_LEVEL", "INFO"),
            trading_windows=env("TRADING_WINDOWS", "03:00-12:00,08:00-17:00"),
            entry_wait_seconds=env_int("ENTRY_WAIT_SECONDS", 60),
            entry_poll_ms=env_int("ENTRY_POLL_MS", 500),
            entry_buffer_points=env_float("ENTRY_BUFFER_POINTS", 0.0),
            accounts_json=env("ACCOUNTS_JSON", "[]"),
            # Advanced settings
            dedup_ttl_seconds=env_float("DEDUP_TTL_SECONDS", 120.0),
            enable_notifications=env_bool("ENABLE_NOTIFICATIONS", True),
            enable_advanced_trade_mgmt=env_bool("ENABLE_ADVANCED_TRADE_MGMT", True),
            scalp_tp1_percent=env_float("SCALP_TP1_PERCENT", 70.0),
            scalp_tp2_percent=env_float("SCALP_TP2_PERCENT", 100.0),
            long_tp1_percent=env_float("LONG_TP1_PERCENT", 50.0),
            long_tp2_percent=env_float("LONG_TP2_PERCENT", 30.0),
            enable_breakeven=env_bool("ENABLE_BREAKEVEN", True),
            breakeven_offset_pips=env_float("BREAKEVEN_OFFSET_PIPS", 3.0),
            enable_trailing=env_bool("ENABLE_TRAILING", True),
            trailing_activation_pips=env_float("TRAILING_ACTIVATION_PIPS", 30.0),
            trailing_stop_pips=env_float("TRAILING_STOP_PIPS", 15.0),
            enable_addon=env_bool("ENABLE_ADDON", True),
            addon_max_count=env_int("ADDON_MAX_COUNT", 2),
            addon_lot_factor=env_float("ADDON_LOT_FACTOR", 0.5),
        )

    def accounts(self) -> list[dict[str, Any]]:
        return json.loads(self.accounts_json)


# FAST signal update window (seconds)
FAST_UPDATE_WINDOW_SECONDS = float(os.getenv("FAST_UPDATE_WINDOW_SECONDS", "30"))
