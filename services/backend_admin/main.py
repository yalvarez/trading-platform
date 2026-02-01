# --- SETTINGS PUT/DELETE ---
@app.put("/settings/{key}", dependencies=[Depends(check_auth)])
def update_setting(key: str, setting: Setting, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("UPDATE settings SET value=%s WHERE key=%s", (setting.value, key))
        db.commit()
    return {"ok": True}

@app.delete("/settings/{key}", dependencies=[Depends(check_auth)])
def delete_setting(key: str, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("DELETE FROM settings WHERE key=%s", (key,))
        db.commit()
    return {"ok": True}

# --- ACCOUNTS PUT/DELETE ---
@app.put("/accounts/{account_id}", dependencies=[Depends(check_auth)])
def update_account(account_id: int, account: Account, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("UPDATE accounts SET name=%s, host=%s, port=%s, active=%s, fixed_lot=%s, chat_id=%s, trading_mode=%s WHERE id=%s",
            (account.name, account.host, account.port, account.active, account.fixed_lot, account.chat_id, account.trading_mode, account_id))
        db.commit()
    return {"ok": True}

@app.delete("/accounts/{account_id}", dependencies=[Depends(check_auth)])
def delete_account(account_id: int, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("DELETE FROM accounts WHERE id=%s", (account_id,))
        db.commit()
    return {"ok": True}

# --- CHANNELS PUT/DELETE ---
@app.put("/channels/{channel_id}", dependencies=[Depends(check_auth)])
def update_channel(channel_id: int, channel: Channel, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("UPDATE channels SET name=%s, description=%s WHERE id=%s", (channel.name, channel.description, channel_id))
        db.commit()
    return {"ok": True}

@app.delete("/channels/{channel_id}", dependencies=[Depends(check_auth)])
def delete_channel(channel_id: int, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("DELETE FROM channels WHERE id=%s", (channel_id,))
        db.commit()
    return {"ok": True}

# --- PROVIDERS PUT/DELETE ---
@app.put("/providers/{provider_id}", dependencies=[Depends(check_auth)])
def update_provider(provider_id: int, provider: Provider, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("UPDATE signal_providers SET name=%s, parser=%s WHERE id=%s", (provider.name, provider.parser, provider_id))
        db.commit()
    return {"ok": True}

@app.delete("/providers/{provider_id}", dependencies=[Depends(check_auth)])
def delete_provider(provider_id: int, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("DELETE FROM signal_providers WHERE id=%s", (provider_id,))
        db.commit()
    return {"ok": True}

# --- ACCOUNT_CHANNELS DELETE ---
@app.delete("/account_channels", dependencies=[Depends(check_auth)])
def delete_account_channel(ac: AccountChannel, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("DELETE FROM account_channels WHERE account_id=%s AND channel_id=%s", (ac.account_id, ac.channel_id))
        db.commit()
    return {"ok": True}

# --- CHANNEL_PROVIDERS DELETE ---
@app.delete("/channel_providers", dependencies=[Depends(check_auth)])
def delete_channel_provider(cp: ChannelProvider, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("DELETE FROM channel_providers WHERE channel_id=%s AND provider_id=%s", (cp.channel_id, cp.provider_id))
        db.commit()
    return {"ok": True}
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import psycopg2
import os
from typing import List, Optional
from models import Setting, Account, Channel, Provider, AccountChannel, ChannelProvider

app = FastAPI()
security = HTTPBasic()

# Simple auth (replace with env vars or a better method in production)
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin")

DB_URL = os.getenv("CONFIG_DB_URL", "postgresql://user:password@db:5432/config")

def get_db():
    conn = psycopg2.connect(DB_URL)
    try:
        yield conn
    finally:
        conn.close()

def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != ADMIN_USER or credentials.password != ADMIN_PASS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

# --- ENDPOINTS ---
@app.get("/settings", dependencies=[Depends(check_auth)])
def list_settings(db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("SELECT key, value FROM settings")
        return [Setting(key=row[0], value=row[1]) for row in cur.fetchall()]

@app.post("/settings", dependencies=[Depends(check_auth)])
def set_setting(setting: Setting, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (setting.key, setting.value))
        db.commit()
    return {"ok": True}

@app.get("/accounts", response_model=List[Account], dependencies=[Depends(check_auth)])
def list_accounts(db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("SELECT id, name, host, port, active, fixed_lot, chat_id, trading_mode FROM accounts")
        return [Account(id=row[0], name=row[1], host=row[2], port=row[3], active=row[4], fixed_lot=row[5], chat_id=row[6], trading_mode=row[7]) for row in cur.fetchall()]

@app.post("/accounts", dependencies=[Depends(check_auth)])
def add_account(account: Account, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("INSERT INTO accounts (name, host, port, active, fixed_lot, chat_id, trading_mode) VALUES (%s, %s, %s, %s, %s, %s, %s)", (account.name, account.host, account.port, account.active, account.fixed_lot, account.chat_id, account.trading_mode))
        db.commit()
    return {"ok": True}


# --- CHANNELS ---
@app.get("/channels", response_model=List[Channel], dependencies=[Depends(check_auth)])
def list_channels(db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("SELECT id, name, description FROM channels")
        return [Channel(id=row[0], name=row[1], description=row[2]) for row in cur.fetchall()]

@app.post("/channels", dependencies=[Depends(check_auth)])
def add_channel(channel: Channel, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("INSERT INTO channels (name, description) VALUES (%s, %s)", (channel.name, channel.description))
        db.commit()
    return {"ok": True}

# --- PROVIDERS ---
@app.get("/providers", response_model=List[Provider], dependencies=[Depends(check_auth)])
def list_providers(db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("SELECT id, name, parser FROM signal_providers")
        return [Provider(id=row[0], name=row[1], parser=row[2]) for row in cur.fetchall()]

@app.post("/providers", dependencies=[Depends(check_auth)])
def add_provider(provider: Provider, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("INSERT INTO signal_providers (name, parser) VALUES (%s, %s)", (provider.name, provider.parser))
        db.commit()
    return {"ok": True}

# --- ACCOUNT_CHANNELS ---
@app.get("/account_channels", response_model=List[AccountChannel], dependencies=[Depends(check_auth)])
def list_account_channels(db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("SELECT account_id, channel_id FROM account_channels")
        return [AccountChannel(account_id=row[0], channel_id=row[1]) for row in cur.fetchall()]

@app.post("/account_channels", dependencies=[Depends(check_auth)])
def add_account_channel(ac: AccountChannel, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("INSERT INTO account_channels (account_id, channel_id) VALUES (%s, %s)", (ac.account_id, ac.channel_id))
        db.commit()
    return {"ok": True}

# --- CHANNEL_PROVIDERS ---
@app.get("/channel_providers", response_model=List[ChannelProvider], dependencies=[Depends(check_auth)])
def list_channel_providers(db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("SELECT channel_id, provider_id FROM channel_providers")
        return [ChannelProvider(channel_id=row[0], provider_id=row[1]) for row in cur.fetchall()]

@app.post("/channel_providers", dependencies=[Depends(check_auth)])
def add_channel_provider(cp: ChannelProvider, db=Depends(get_db)):
    with db.cursor() as cur:
        cur.execute("INSERT INTO channel_providers (channel_id, provider_id) VALUES (%s, %s)", (cp.channel_id, cp.provider_id))
        db.commit()
    return {"ok": True}
