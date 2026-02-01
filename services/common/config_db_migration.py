
import os
import json
import psycopg2

def run_schema_sql(db_url, schema_path):
    with open(schema_path, 'r', encoding='utf-8') as f:
        sql = f.read()
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    # Ejecutar múltiples sentencias separadas por ';'
    for statement in sql.split(';'):
        stmt = statement.strip()
        if stmt:
            cur.execute(stmt)
    conn.commit()
    cur.close()
    conn.close()

def migrate_env_to_db(db_url):
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    # 1. Migrar settings generales
    # keys = [
    #     'DEFAULT_SL_XAUUSD_PIPS','REDIS_URL','TG_API_ID','TG_API_HASH','TG_PHONE','TELEGRAM_INGESTOR_URL','TG_TEST_CHAT_ID','LOG_LEVEL','TRADING_WINDOWS','SCALING_TRAMO_PIPS','SCALING_PERCENT_PER_TRAMO','ENTRY_WAIT_SECONDS','ENTRY_POLL_MS','ENTRY_BUFFER_POINTS','MT5_WEB_USER','MT5_WEB_PASS','DEDUP_TTL_SECONDS','ENABLE_NOTIFICATIONS','ENABLE_ADVANCED_TRADE_MGMT','SCALP_TP1_PERCENT','SCALP_TP2_PERCENT','LONG_TP1_PERCENT','LONG_TP2_PERCENT','ENABLE_BREAKEVEN','BREAKEVEN_OFFSET_PIPS','ENABLE_TRAILING','TRAILING_ACTIVATION_PIPS','TRAILING_STOP_PIPS','ENABLE_ADDON','ADDON_MAX_COUNT','ADDON_LOT_FACTOR','FAST_UPDATE_WINDOW_SECONDS'
    # ]
    # for key in keys:
    #     val = os.environ.get(key)
    #     if val is not None:
    #         cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, val))
    
    # 2. Migrar cuentas
    accounts_json = os.environ.get('ACCOUNTS_JSON')
    print(f"[DEBUG] ACCOUNTS_JSON={accounts_json}")
    if accounts_json:
        accounts = json.loads(accounts_json)
        for acc in accounts:
            cur.execute("""
                INSERT INTO accounts (name, host, port, active, fixed_lot, chat_id, trading_mode)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (acc['name'], acc['host'], acc['port'], acc['active'], acc['fixed_lot'], acc['chat_id'], acc['trading_mode']))
            acc_id = cur.fetchone()[0]
            for ch in acc.get('allowed_channels', []):
                cur.execute("INSERT INTO account_channels (account_id, channel_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (acc_id, ch))
    # 3. Migrar canales y proveedores
    channels_json = os.environ.get('CHANNELS_CONFIG_JSON')
    print(f"[DEBUG] CHANNELS_CONFIG_JSON={channels_json}")
    if channels_json:
        channels = json.loads(channels_json)
        # Insertar proveedores únicos
        all_providers = set()
        for provs in channels.values():
            all_providers.update(provs)
        provider_ids = {}
        for prov in all_providers:
            cur.execute("INSERT INTO signal_providers (name, parser) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING RETURNING id", (prov, prov))
            res = cur.fetchone()
            if res:
                provider_ids[prov] = res[0]
            else:
                cur.execute("SELECT id FROM signal_providers WHERE name=%s", (prov,))
                provider_ids[prov] = cur.fetchone()[0]
        # Insertar mapeo canal-proveedor
        for ch, provs in channels.items():
            for prov in provs:
                cur.execute("INSERT INTO channel_providers (channel_id, provider_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (int(ch), provider_ids[prov]))
    conn.commit()
    cur.close()
    conn.close()

if __name__ == "__main__":
    db_url = os.environ.get("CONFIG_DB_URL", "postgresql://trading_user:trading_pass@localhost:5432/trading_config")
    schema_path = os.path.join(os.path.dirname(__file__), "config_db_schema_full.sql")
    #run_schema_sql(db_url, schema_path)
    migrate_env_to_db(db_url)
