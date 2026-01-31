import os
import psycopg2


class ConfigProvider:
    def __init__(self, db_url=None):
        self.db_url = db_url or os.environ.get("CONFIG_DB_URL")
        self._conn = None
        if self.db_url:
            self._conn = psycopg2.connect(self.db_url)

    def get(self, key, default=None):
        if self._conn:
            with self._conn.cursor() as cur:
                cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
                row = cur.fetchone()
                return row[0] if row else default
        return os.environ.get(key, default)

    def set(self, key, value):
        if self._conn:
            with self._conn.cursor() as cur:
                cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))
                self._conn.commit()
        else:
            os.environ[key] = value

    def get_accounts(self):
        if self._conn:
            with self._conn.cursor() as cur:
                cur.execute("SELECT id, name, host, port, active, fixed_lot, chat_id, trading_mode FROM accounts")
                accounts = []
                for row in cur.fetchall():
                    account = {
                        "id": row[0], "name": row[1], "host": row[2], "port": row[3], "active": row[4],
                        "fixed_lot": row[5], "chat_id": row[6], "trading_mode": row[7], "allowed_channels": []
                    }
                    cur.execute("SELECT channel_id FROM account_channels WHERE account_id = %s", (row[0],))
                    account["allowed_channels"] = [r[0] for r in cur.fetchall()]
                    accounts.append(account)
                return accounts
        # fallback a variable de entorno
        import json
        return json.loads(os.environ.get("ACCOUNTS_JSON", "[]"))

    def get_signal_providers(self):
        if self._conn:
            with self._conn.cursor() as cur:
                cur.execute("SELECT id, name, parser FROM signal_providers")
                providers = []
                for row in cur.fetchall():
                    providers.append({"id": row[0], "name": row[1], "parser": row[2]})
                return providers
        # fallback: parse from CHANNELS_CONFIG_JSON
        import json
        channels = json.loads(os.environ.get("CHANNELS_CONFIG_JSON", "{}"))
        all_providers = set()
        for provs in channels.values():
            all_providers.update(provs)
        return [{"name": p, "parser": p} for p in all_providers]

    def get_account_channels(self, account_id):
        if self._conn:
            with self._conn.cursor() as cur:
                cur.execute("SELECT channel_id FROM account_channels WHERE account_id = %s", (account_id,))
                return [row[0] for row in cur.fetchall()]
        # fallback: buscar en ACCOUNTS_JSON
        for acc in self.get_accounts():
            if acc.get("id") == account_id:
                return acc.get("allowed_channels", [])
        return []

    def get_channel_providers(self):
        if self._conn:
            with self._conn.cursor() as cur:
                cur.execute("SELECT channel_id, provider_id FROM channel_providers")
                mapping = {}
                for row in cur.fetchall():
                    mapping.setdefault(row[0], []).append(row[1])
                return mapping
        # fallback: parse from CHANNELS_CONFIG_JSON
        import json
        channels = json.loads(os.environ.get("CHANNELS_CONFIG_JSON", "{}"))
        mapping = {}
        for ch, provs in channels.items():
            mapping[int(ch)] = provs
        return mapping

    def close(self):
        if self._conn:
            self._conn.close()
