import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))  # so `import app` / sibling imports work like the existing app.py does

import pytest
import httpx

from services.router_parser.app import SignalRouter, forward_to_n8n


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.mark.asyncio
async def test_signal_router_has_no_channels_config_param():
    r = SignalRouter(FakeRedis(), dedup_ttl=120.0)
    assert not hasattr(r, "channels_config")


@pytest.mark.asyncio
async def test_parse_signal_tries_the_single_parser():
    r = SignalRouter(FakeRedis(), dedup_ttl=120.0)
    result = r.parse_signal("XAUUSD BUY NOW", chat_id="-1003321565807")
    assert result is not None
    assert result.symbol == "XAUUSD"
    assert result.direction == "BUY"


@pytest.mark.asyncio
async def test_parse_signal_returns_none_for_unrecognized_text():
    r = SignalRouter(FakeRedis(), dedup_ttl=120.0)
    result = r.parse_signal("Spam your feedbacks @trader_ahmed_2", chat_id="-1003321565807")
    assert result is None


@pytest.mark.asyncio
async def test_forward_to_n8n_posts_expected_payload(monkeypatch):
    captured = {}

    async def fake_post(self, url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        class R:
            status_code = 200
        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    await forward_to_n8n("HIT SL. GET READY FOR RECOVERY", "-1003321565807", "https://n8n.example.com/in")

    assert captured["url"] == "https://n8n.example.com/in"
    assert captured["json"]["chat_id"] == "-1003321565807"
    assert captured["json"]["text"] == "HIT SL. GET READY FOR RECOVERY"
    assert "timestamp" in captured["json"]


@pytest.mark.asyncio
async def test_forward_to_n8n_swallows_errors(monkeypatch):
    async def fake_post(self, url, json=None, timeout=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    # Must not raise
    await forward_to_n8n("some text", "-1", "https://n8n.example.com/in")
