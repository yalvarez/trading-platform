
# ConfigProvider abstraction
from services.common.config_db import ConfigProvider
import json

config = ConfigProvider()
FAST_UPDATE_WINDOW_SECONDS = float(config.get("FAST_UPDATE_WINDOW_SECONDS", 30))
CHANNELS_CONFIG_JSON = config.get("CHANNELS_CONFIG_JSON", "{}")

class Settings:
    @staticmethod
    def load():
        return {
            "redis_url": config.get("REDIS_URL", "redis://redis:6379/0"),
            "log_level": config.get("LOG_LEVEL", "INFO"),
            "trading_windows": config.get("TRADING_WINDOWS", "03:00-12:00,08:00-17:00"),
            "entry_wait_seconds": int(config.get("ENTRY_WAIT_SECONDS", 60)),
            "entry_poll_ms": int(config.get("ENTRY_POLL_MS", 500)),
            "entry_buffer_points": float(config.get("ENTRY_BUFFER_POINTS", 0.0)),
            "dedup_ttl_seconds": float(config.get("DEDUP_TTL_SECONDS", 120.0)),
            "enable_notifications": config.get("ENABLE_NOTIFICATIONS", "true") in ("true", "1", "yes", "on"),
            "enable_advanced_trade_mgmt": config.get("ENABLE_ADVANCED_TRADE_MGMT", "true") in ("true", "1", "yes", "on"),
            "scalp_tp1_percent": float(config.get("SCALP_TP1_PERCENT", 70.0)),
            "scalp_tp2_percent": float(config.get("SCALP_TP2_PERCENT", 100.0)),
            "long_tp1_percent": float(config.get("LONG_TP1_PERCENT", 50.0)),
            "long_tp2_percent": float(config.get("LONG_TP2_PERCENT", 30.0)),
            "enable_breakeven": config.get("ENABLE_BREAKEVEN", "true") in ("true", "1", "yes", "on"),
            "breakeven_offset_pips": float(config.get("BREAKEVEN_OFFSET_PIPS", 3.0)),
            "enable_trailing": config.get("ENABLE_TRAILING", "true") in ("true", "1", "yes", "on"),
            "trailing_activation_pips": float(config.get("TRAILING_ACTIVATION_PIPS", 30.0)),
            "trailing_stop_pips": float(config.get("TRAILING_STOP_PIPS", 15.0)),
            "enable_addon": config.get("ENABLE_ADDON", "true") in ("true", "1", "yes", "on"),
            "addon_max_count": int(config.get("ADDON_MAX_COUNT", 2)),
            "addon_lot_factor": float(config.get("ADDON_LOT_FACTOR", 0.5)),
            "fast_update_window_seconds": float(config.get("FAST_UPDATE_WINDOW_SECONDS", 30)),
        }

    @staticmethod
    def accounts():
        # Load from DB if available, else fallback to env
        import psycopg2
        db_url = config.db_url
        if db_url:
            conn = psycopg2.connect(db_url)
            from services.common.config_db_loader import load_accounts
            return load_accounts(conn)
        return json.loads(config.get("ACCOUNTS_JSON", "[]"))

    @staticmethod
    def signal_providers():
        import psycopg2
        db_url = config.db_url
        if db_url:
            conn = psycopg2.connect(db_url)
            from services.common.config_db_loader import load_signal_providers
            return load_signal_providers(conn)
        # fallback: parse from CHANNELS_CONFIG_JSON
        return []

    @staticmethod
    def channel_providers():
        import psycopg2
        db_url = config.db_url
        if db_url:
            conn = psycopg2.connect(db_url)
            from services.common.config_db_loader import load_channel_providers
            return load_channel_providers(conn)
        # fallback: parse from CHANNELS_CONFIG_JSON
        return {}
