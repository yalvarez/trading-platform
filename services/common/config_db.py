"""
config_db.py
Proveedor de configuracion basado unicamente en variables de entorno.
Sustituye la version anterior que soportaba un backend Postgres —
eliminado junto con backend_admin.
"""
import json
import logging
import os
from typing import Any

log = logging.getLogger("config_db")


class ConfigProvider:
    """Lee/escribe configuracion desde variables de entorno."""

    def get(self, key: str, default: Any = None) -> Any:
        return os.environ.get(key, default)

    def set(self, key: str, value: str) -> None:
        os.environ[key] = value

    def get_accounts(self) -> list[dict]:
        return json.loads(os.environ.get("ACCOUNTS_JSON", "[]"))

    def get_signal_providers(self) -> list[dict]:
        return []

    def get_account_channels(self, account_id: int) -> list[int]:
        for acc in self.get_accounts():
            if acc.get("id") == account_id:
                return acc.get("allowed_channels", [])
        return []

    def get_channel_providers(self) -> dict[int, list]:
        return {}

    def close(self) -> None:
        pass
