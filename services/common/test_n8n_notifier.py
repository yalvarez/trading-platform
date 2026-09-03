import pytest
import httpx
from services.common.n8n_notifier import N8nWebhookNotifier


class DummyResponse:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.mark.asyncio
async def test_send_event_posts_json_and_returns_true_on_success(monkeypatch):
    captured = {}

    async def fake_post(self, url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return DummyResponse(200)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    notifier = N8nWebhookNotifier(webhook_url="https://n8n.example.com/webhook/trades")
    ok = await notifier.send_event("trade_opened", ticket=123, symbol="XAUUSD")

    assert ok is True
    assert captured["url"] == "https://n8n.example.com/webhook/trades"
    assert captured["json"] == {"event": "trade_opened", "ticket": 123, "symbol": "XAUUSD"}


@pytest.mark.asyncio
async def test_send_event_includes_token_header_when_set(monkeypatch):
    captured = {}

    async def fake_post(self, url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return DummyResponse(200)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    notifier = N8nWebhookNotifier(webhook_url="https://n8n.example.com/webhook/trades", token="secret123")
    await notifier.send_event("trade_closed", ticket=456)

    assert captured["headers"]["X-N8N-Token"] == "secret123"


@pytest.mark.asyncio
async def test_send_event_returns_false_and_does_not_raise_on_error(monkeypatch):
    async def fake_post(self, url, json=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    notifier = N8nWebhookNotifier(webhook_url="https://n8n.example.com/webhook/trades")
    ok = await notifier.send_event("trade_opened", ticket=789)

    assert ok is False
