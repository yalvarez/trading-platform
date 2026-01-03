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

@dataclass
class Settings:
    redis_url: str
    log_level: str
    trading_windows: str
    entry_wait_seconds: int
    entry_poll_ms: int
    entry_buffer_points: float
    accounts_json: str

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
        )

    def accounts(self) -> list[dict[str, Any]]:
        return json.loads(self.accounts_json)
