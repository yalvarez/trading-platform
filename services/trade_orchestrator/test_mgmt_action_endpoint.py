import os
import pytest
from fastapi.testclient import TestClient

os.environ["N8N_ACTION_API_KEY"] = "test-action-key"

from tests.test_simulador_mt5 import SimuladorMT5
from services.trade_orchestrator.trade_manager import TradeManager
from services.trade_orchestrator.mgmt_api import create_mgmt_app

HEADERS = {"X-N8N-Action-Key": "test-action-key"}
ACCOUNT = {"name": "demo", "active": True, "host": "x", "port": 1}
CHAT_ID = "-1001234567890"


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
    resp = client.post("/mgmt/action", json={"action": "close_now", "chat_id": CHAT_ID, "raw_text": "close now", "correction": None})
    assert resp.status_code == 401


def test_mgmt_action_no_active_trade_returns_200(tm_and_client):
    tm, client = tm_and_client
    resp = client.post("/mgmt/action", headers=HEADERS, json={"action": "close_now", "chat_id": CHAT_ID, "raw_text": "close now", "correction": None})
    assert resp.status_code == 200
    assert resp.json()["status"] == "no_active_trade"


def test_mgmt_action_rejects_request_missing_chat_id(tm_and_client):
    """The old `symbol` field is no longer accepted in place of chat_id -- a
    request without chat_id must fail Pydantic validation (422), not be
    silently treated as chat_id=None."""
    tm, client = tm_and_client
    resp = client.post("/mgmt/action", headers=HEADERS, json={"action": "close_now", "symbol": "XAUUSD", "raw_text": "close now", "correction": None})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_mgmt_action_close_now_closes_group(tm_and_client):
    tm, client = tm_and_client
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)

    resp = client.post("/mgmt/action", headers=HEADERS, json={"action": "close_now", "chat_id": CHAT_ID, "raw_text": "close now", "correction": None})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["results"][0]["status"] == "closed"
    assert len(tm.trades) == 0


@pytest.mark.asyncio
async def test_mgmt_action_close_now_only_affects_the_matching_chat_id(tm_and_client):
    tm, client = tm_and_client
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id=CHAT_ID)
    await tm.open_group(ACCOUNT, symbol="XAUUSD", direction="BUY", sl=2490.0, tp1=2510.0, tp2=2530.0, chat_id="other-chat")

    resp = client.post("/mgmt/action", headers=HEADERS, json={"action": "close_now", "chat_id": CHAT_ID, "raw_text": "close now", "correction": None})

    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    remaining_chats = {t.chat_id for t in tm.trades.values()}
    assert remaining_chats == {"other-chat"}


# --- Final fix wave (2026-09-11), Fix 2: percent bounds at the HTTP boundary ---
# percent originates from an LLM (Ollama) extraction of free-form Telegram
# text, so a garbage value is a realistic input and must never reach
# apply_mgmt_action.


@pytest.mark.parametrize("bad_percent", [-50.0, 0.0, 100.0, 150.0])
def test_mgmt_action_rejects_out_of_range_percent(tm_and_client, bad_percent):
    tm, client = tm_and_client
    resp = client.post("/mgmt/action", headers=HEADERS, json={
        "action": "close_partial_now", "chat_id": CHAT_ID,
        "raw_text": "cierra algo", "correction": None, "percent": bad_percent,
    })
    assert resp.status_code == 422


def test_mgmt_action_accepts_a_valid_percent(tm_and_client):
    tm, client = tm_and_client
    resp = client.post("/mgmt/action", headers=HEADERS, json={
        "action": "close_partial_now", "chat_id": CHAT_ID,
        "raw_text": "cierra 30%", "correction": None, "percent": 30.0,
    })
    assert resp.status_code == 200


def test_mgmt_action_still_accepts_an_omitted_percent(tm_and_client):
    """percent unset means 'use the 50% default' and must stay valid."""
    tm, client = tm_and_client
    resp = client.post("/mgmt/action", headers=HEADERS, json={
        "action": "close_partial_now", "chat_id": CHAT_ID,
        "raw_text": "cierra parte", "correction": None,
    })
    assert resp.status_code == 200
