import httpx
import pytest
import respx

from services.trade_orchestrator.n8n_event_client import N8nEventClient


@pytest.mark.asyncio
@respx.mock
async def test_post_event_returns_true_on_2xx():
    route = respx.post("https://n8n.example.com/webhook/events").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = N8nEventClient("https://n8n.example.com/webhook/events")

    ok = await client.post_event({"event_id": "1", "event_type": "x", "channel": "audit",
                                   "timestamp": "t", "message": "m", "payload": {}})

    assert ok is True
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_post_event_sends_full_envelope_as_json_body():
    envelope = {"event_id": "1", "event_type": "group_opened", "channel": "both",
                "timestamp": "t", "message": "hi", "payload": {"group_id": 61}}
    route = respx.post("https://n8n.example.com/webhook/events").mock(
        return_value=httpx.Response(200)
    )
    client = N8nEventClient("https://n8n.example.com/webhook/events")

    await client.post_event(envelope)

    assert route.calls.last.request.content
    import json
    sent = json.loads(route.calls.last.request.content)
    assert sent == envelope


@pytest.mark.asyncio
@respx.mock
async def test_post_event_sends_token_header_when_configured():
    route = respx.post("https://n8n.example.com/webhook/events").mock(
        return_value=httpx.Response(200)
    )
    client = N8nEventClient("https://n8n.example.com/webhook/events", token="secret123")

    await client.post_event({"event_id": "1", "event_type": "x", "channel": "audit",
                              "timestamp": "t", "message": "m", "payload": {}})

    assert route.calls.last.request.headers["X-N8N-Token"] == "secret123"


@pytest.mark.asyncio
@respx.mock
async def test_post_event_returns_false_on_non_2xx():
    respx.post("https://n8n.example.com/webhook/events").mock(
        return_value=httpx.Response(500)
    )
    client = N8nEventClient("https://n8n.example.com/webhook/events")

    ok = await client.post_event({"event_id": "1", "event_type": "x", "channel": "audit",
                                   "timestamp": "t", "message": "m", "payload": {}})

    assert ok is False


@pytest.mark.asyncio
@respx.mock
async def test_post_event_returns_false_on_network_error_without_raising():
    respx.post("https://n8n.example.com/webhook/events").mock(side_effect=httpx.ConnectError("boom"))
    client = N8nEventClient("https://n8n.example.com/webhook/events")

    ok = await client.post_event({"event_id": "1", "event_type": "x", "channel": "audit",
                                   "timestamp": "t", "message": "m", "payload": {}})

    assert ok is False
