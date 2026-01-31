import json
import psycopg2

def load_settings(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM settings")
        return {row[0]: row[1] for row in cur.fetchall()}

def load_accounts(conn):
    with conn.cursor() as cur:
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

def load_signal_providers(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, parser FROM signal_providers")
        providers = []
        for row in cur.fetchall():
            providers.append({"id": row[0], "name": row[1], "parser": row[2]})
        return providers

def load_channel_providers(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT channel_id, provider_id FROM channel_providers")
        mapping = {}
        for row in cur.fetchall():
            mapping.setdefault(row[0], []).append(row[1])
        return mapping
