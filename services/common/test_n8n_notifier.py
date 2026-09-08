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
    # El payload siempre se ajusta al esquema de la tabla n8n: group_id, leg,
    # symbol, action, message. "event" mapea a "action"; cualquier campo que
    # no encaje en el esquema (aqui "ticket") se serializa dentro de "message".
    assert captured["json"] == {
        "group_id": None,
        "leg": None,
        "symbol": "XAUUSD",
        "action": "trade_opened",
        "message": '{"ticket": 123}',
    }


@pytest.mark.asyncio
async def test_send_event_fits_full_schema_fields_without_touching_message(monkeypatch):
    captured = {}

    async def fake_post(self, url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return DummyResponse(200)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    notifier = N8nWebhookNotifier(webhook_url="https://n8n.example.com/webhook/trades")
    ok = await notifier.send_event(
        "group_opened", group_id=7, leg="tp1", symbol="XAUUSD", message="Grupo 7 abierto.",
    )

    assert ok is True
    # Cuando todos los campos ya encajan en el esquema, el payload sale exacto,
    # sin nada anexado a message.
    assert captured["json"] == {
        "group_id": 7,
        "leg": "tp1",
        "symbol": "XAUUSD",
        "action": "group_opened",
        "message": "Grupo 7 abierto.",
    }


@pytest.mark.asyncio
async def test_send_event_appends_extra_fields_to_existing_message(monkeypatch):
    captured = {}

    async def fake_post(self, url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return DummyResponse(200)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    notifier = N8nWebhookNotifier(webhook_url="https://n8n.example.com/webhook/trades")
    ok = await notifier.send_event(
        "trailing_updated", group_id=3, ticket=999, peak_multiple=1.5, message="Trailing SL actualizado.",
    )

    assert ok is True
    payload = captured["json"]
    assert payload["group_id"] == 3
    assert payload["leg"] is None
    assert payload["symbol"] is None
    assert payload["action"] == "trailing_updated"
    assert payload["message"].startswith("Trailing SL actualizado. | extra=")
    assert '"ticket": 999' in payload["message"]
    assert '"peak_multiple": 1.5' in payload["message"]


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
