import os
import logging
from typing import Optional, Any
import psycopg2

log = logging.getLogger("config_db")


class ConfigProvider:
    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url or os.environ.get("CONFIG_DB_URL")
        self._conn = None
        if self.db_url:
            try:
                self._conn = psycopg2.connect(self.db_url)
            except Exception as e:
                log.error("[ConfigDB] No se pudo conectar a la base de datos: %s", e)

    def get(self, key: str, default: Any = None) -> Any:
        if self._conn:
            try:
                with self._conn.cursor() as cur:
                    cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
                    row = cur.fetchone()
                    return row[0] if row else default
            except Exception as e:
                log.warning("[ConfigDB] Error leyendo clave '%s': %s. Usando default.", key, e)
        return os.environ.get(key, default)

    def set(self, key: str, value: str) -> None:
        if self._conn:
            try:
                with self._conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                        (key, value),
                    )
                    self._conn.commit()
            except Exception as e:
                log.error("[ConfigDB] Error escribiendo clave '%s': %s", key, e)
        else:
            os.environ[key] = value

    def get_accounts(self) -> list[dict]:
        if self._conn:
            with self._conn.cursor() as cur:
                # Un solo JOIN elimina el N+1 query anterior
                cur.execute("""
                    SELECT a.id, a.name, a.host, a.port, a.active,
                           a.fixed_lot, a.chat_id, a.trading_mode,
                           COALESCE(array_agg(ac.channel_id) FILTER (WHERE ac.channel_id IS NOT NULL), ARRAY[]::bigint[]) AS allowed_channels
                    FROM accounts a
                    LEFT JOIN account_channels ac ON ac.account_id = a.id
                    GROUP BY a.id, a.name, a.host, a.port, a.active, a.fixed_lot, a.chat_id, a.trading_mode
                    ORDER BY a.id
                """)
                accounts = []
                for row in cur.fetchall():
                    accounts.append({
                        "id": row[0], "name": row[1], "host": row[2], "port": row[3],
                        "active": row[4], "fixed_lot": row[5], "chat_id": row[6],
                        "trading_mode": row[7], "allowed_channels": list(row[8]),
                    })
                return accounts
        import json
        return json.loads(os.environ.get("ACCOUNTS_JSON", "[]"))

    def get_signal_providers(self) -> list[dict]:
        if self._conn:
            with self._conn.cursor() as cur:
                cur.execute("SELECT id, name, parser FROM signal_providers")
                return [{"id": row[0], "name": row[1], "parser": row[2]} for row in cur.fetchall()]
        import json
        channels = json.loads(os.environ.get("CHANNELS_CONFIG_JSON", "{}"))
        all_providers: set[str] = set()
        for provs in channels.values():
            all_providers.update(provs)
        return [{"name": p, "parser": p} for p in all_providers]

    def get_account_channels(self, account_id: int) -> list[int]:
        if self._conn:
            with self._conn.cursor() as cur:
                cur.execute("SELECT channel_id FROM account_channels WHERE account_id = %s", (account_id,))
                return [row[0] for row in cur.fetchall()]
        for acc in self.get_accounts():
            if acc.get("id") == account_id:
                return acc.get("allowed_channels", [])
        return []

    def get_channel_providers(self) -> dict[int, list]:
        if self._conn:
            with self._conn.cursor() as cur:
                cur.execute("SELECT channel_id, provider_id FROM channel_providers")
                mapping: dict[int, list] = {}
                for row in cur.fetchall():
                    mapping.setdefault(row[0], []).append(row[1])
                return mapping
        import json
        channels = json.loads(os.environ.get("CHANNELS_CONFIG_JSON", "{}"))
        return {int(ch): provs for ch, provs in channels.items()}

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
