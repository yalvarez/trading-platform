import os
import pytest
from fastapi.testclient import TestClient

os.environ["N8N_ACTION_API_KEY"] = "test-action-key"

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.trade_manager import TradeManager
from services.trade_orchestrator.mgmt_api import create_mgmt_app

HEADERS = {"X-N8N-Action-Key": "test-action-key"}
ACCOUNT = {"name": "demo", "active": True, "host": "x", "port": 1}


class DummyExecutor:
    def __init__(self, sim):
        self.sim = sim
        self.accounts = [ACCOUNT]

    def _client_for(self, account):
        return self.sim


class DummyNotifier:
    async def notify_trade_event(self, event, **kwargs):
        pass

    async def notify(self, target, message):
        pass


@pytest.fixture
def tm_and_client():
    sim = SimuladorMT5()
    sim.price = 2500.0
    tm = TradeManager(DummyExecutor(sim), notifier=DummyNotifier())
    app = create_mgmt_app(tm)
    return tm, TestClient(app)


def test_mgmt_action_requires_api_key(tm_and_client):
    tm, client = tm_and_client
    resp = client.post("/mgmt/action", json={"action": "close_now", "symbol": "XAUUSD", "raw_text": "close now", "correction": None})
    assert resp.status_code == 401


def test_mgmt_action_no_active_trade_returns_200(tm_and_client):
    tm, client = tm_and_client
    resp = client.post("/mgmt/action", headers=HEADERS, json={"action": "close_now", "symbol": "XAUUSD", "raw_text": "close now", "correction": None})
    assert resp.status_code == 200
    assert resp.json()["status"] == "no_active_trade"


@pytest.mark.asyncio
async def test_mgmt_action_close_now_closes_group(tm_and_client):
    tm, client = tm_and_client
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0)

    resp = client.post("/mgmt/action", headers=HEADERS, json={"action": "close_now", "symbol": "XAUUSD", "raw_text": "close now", "correction": None})

    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"
    assert len(tm.trades) == 0
