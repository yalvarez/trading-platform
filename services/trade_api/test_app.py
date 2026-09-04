import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest
from fastapi.testclient import TestClient

os.environ["TRADE_API_KEY"] = "test-key-123"
os.environ["ACCOUNTS_JSON"] = '[{"name": "acct1", "host": "mt5_acct1", "port": 9081, "active": true}]'

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_api.app import app, get_mt5_client

HEADERS = {"X-API-Key": "test-key-123"}


@pytest.fixture
def client():
    sim = SimuladorMT5()
    sim.price = 2500.0
    app.dependency_overrides[get_mt5_client] = lambda: sim
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health_does_not_require_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_open_trade_requires_api_key(client):
    resp = client.post("/trades", json={"symbol": "XAUUSD", "direction": "BUY", "volume": 0.05, "sl": 2490.0, "tp": 2510.0})
    assert resp.status_code == 401


def test_open_trade_creates_position(client):
    resp = client.post(
        "/trades", headers=HEADERS,
        json={"symbol": "XAUUSD", "direction": "BUY", "volume": 0.05, "sl": 2490.0, "tp": 2510.0},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["ticket"] > 0
    assert body["symbol"] == "XAUUSD"


def test_get_trades_lists_open_positions(client):
    client.post("/trades", headers=HEADERS, json={"symbol": "XAUUSD", "direction": "BUY", "volume": 0.05, "sl": 2490.0, "tp": 2510.0})
    resp = client.get("/trades", headers=HEADERS)
    assert resp.status_code == 200
    trades = resp.json()
    assert len(trades) == 1
    assert trades[0]["symbol"] == "XAUUSD"


def test_modify_trade_updates_sl_tp(client):
    open_resp = client.post("/trades", headers=HEADERS, json={"symbol": "XAUUSD", "direction": "BUY", "volume": 0.05, "sl": 2490.0, "tp": 2510.0})
    ticket = open_resp.json()["ticket"]
    resp = client.patch(f"/trades/{ticket}", headers=HEADERS, json={"sl": 2495.0})
    assert resp.status_code == 200
    assert resp.json()["sl"] == 2495.0


def test_close_trade_returns_ok(client):
    open_resp = client.post("/trades", headers=HEADERS, json={"symbol": "XAUUSD", "direction": "BUY", "volume": 0.05, "sl": 2490.0, "tp": 2510.0})
    ticket = open_resp.json()["ticket"]
    resp = client.delete(f"/trades/{ticket}", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"


def test_close_unknown_ticket_returns_404(client):
    resp = client.delete("/trades/999999", headers=HEADERS)
    assert resp.status_code == 404
