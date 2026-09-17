import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(__file__))  # so `import app` / sibling imports work like the existing app.py does

import asyncio
import json

import pytest
import httpx

from services.router_parser.app import SignalRouter, forward_to_n8n, DUPLICATE_SIGNAL, execute_close_now_directly, dispatch_raw_message


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)

    async def set(self, key, value, ex=None, nx=False):
        # Mirrors real Redis SET NX: only creates+returns True if key is absent.
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True


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
    assert captured["json"]["message"] == "HIT SL. GET READY FOR RECOVERY"
    assert "timestamp" in captured["json"]


@pytest.mark.asyncio
async def test_forward_to_n8n_swallows_errors(monkeypatch):
    async def fake_post(self, url, json=None, timeout=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    # Must not raise
    await forward_to_n8n("some text", "-1", "https://n8n.example.com/in")


@pytest.mark.asyncio
async def test_process_raw_signal_returns_duplicate_sentinel_not_none_for_repeated_fast_signal():
    """
    Real production bug: a recognized-but-repeated fast signal ("XAUUSD SELL
    NOW" sent twice within DEDUP_TTL_SECONDS) was returning None from
    process_raw_signal, indistinguishable from "text the parser never
    recognized" -- so app.py's loop_signals forwarded it to n8n as noise on
    the inbound (Ollama) webhook. It must come back as the DUPLICATE_SIGNAL
    sentinel instead, so callers can tell "already handled, drop silently"
    apart from "never recognized, forward to n8n".
    """
    r = SignalRouter(FakeRedis(), dedup_ttl=120.0)

    first = await r.process_raw_signal("-1003321565807", "XAUUSD SELL NOW")
    assert first is not None
    assert first is not DUPLICATE_SIGNAL
    assert first["symbol"] == "XAUUSD"

    second = await r.process_raw_signal("-1003321565807", "XAUUSD SELL NOW")
    assert second is DUPLICATE_SIGNAL


@pytest.mark.asyncio
async def test_process_raw_signal_still_returns_none_for_truly_unrecognized_text():
    r = SignalRouter(FakeRedis(), dedup_ttl=120.0)
    result = await r.process_raw_signal("-1003321565807", "Spam your feedbacks @trader_ahmed_2")
    assert result is None


class FakeRedisWithQueue(FakeRedis):
    def __init__(self):
        super().__init__()
        self.queue = []

    async def rpush(self, key, value):
        self.queue.append((key, value))


@pytest.mark.asyncio
async def test_execute_close_now_directly_posts_expected_payload(monkeypatch):
    captured = {}

    async def fake_post(self, url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        class R:
            status_code = 200
        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    ok = await execute_close_now_directly(
        chat_id="-1003321565807", text="XAUUSD SELL TRADE INVALID / Close now",
        direction_hint="SELL", mgmt_url="http://trade_orchestrator:8200/mgmt/action",
        action_api_key="test-key", redis_client=FakeRedisWithQueue(),
    )

    assert ok is True
    assert captured["url"] == "http://trade_orchestrator:8200/mgmt/action"
    assert captured["json"]["action"] == "close_now"
    assert captured["json"]["chat_id"] == "-1003321565807"
    assert captured["json"]["direction_hint"] == "SELL"
    assert captured["headers"]["X-N8N-Action-Key"] == "test-key"


@pytest.mark.asyncio
async def test_execute_close_now_directly_retries_then_succeeds(monkeypatch):
    calls = {"count": 0}

    async def fake_post(self, url, json=None, headers=None, timeout=None):
        calls["count"] += 1
        class R:
            status_code = 200 if calls["count"] == 3 else 500
        return R()

    async def fake_sleep(seconds):
        pass  # don't actually wait in tests

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    ok = await execute_close_now_directly(
        chat_id="-1", text="TRADE INVALID / Close now", direction_hint=None,
        mgmt_url="http://x/mgmt/action", action_api_key="k", redis_client=FakeRedisWithQueue(),
    )

    assert ok is True
    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_execute_close_now_directly_does_not_retry_on_4xx(monkeypatch):
    calls = {"count": 0}

    async def fake_post(self, url, json=None, headers=None, timeout=None):
        calls["count"] += 1
        class R:
            status_code = 401
        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    redis = FakeRedisWithQueue()
    ok = await execute_close_now_directly(
        chat_id="-1", text="TRADE INVALID / Close now", direction_hint=None,
        mgmt_url="http://x/mgmt/action", action_api_key="wrong-key", redis_client=redis,
    )

    assert ok is False
    assert calls["count"] == 1
    assert len(redis.queue) == 1


@pytest.mark.asyncio
async def test_execute_close_now_directly_enqueues_notification_after_exhausting_retries(monkeypatch):
    async def fake_post(self, url, json=None, headers=None, timeout=None):
        class R:
            status_code = 500
        return R()

    async def fake_sleep(seconds):
        pass

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    redis = FakeRedisWithQueue()
    ok = await execute_close_now_directly(
        chat_id="-1003321565807", text="TRADE INVALID / Close now", direction_hint="SELL",
        mgmt_url="http://x/mgmt/action", action_api_key="k", redis_client=redis,
    )

    assert ok is False
    assert len(redis.queue) == 1
    key, raw = redis.queue[0]
    assert key == "n8n_event_retry_queue"
    item = json.loads(raw)
    envelope = item["envelope"]
    assert envelope["event_type"] == "mgmt_direct_close_failed"
    assert envelope["channel"] == "both"
    assert "-1003321565807" in envelope["message"]
    assert "REVISAR LA CUENTA MANUALMENTE" in envelope["message"]
    assert envelope["payload"]["chat_id"] == "-1003321565807"


class RecordingRouter:
    """Stand-in for SignalRouter that returns a fixed process_raw_signal result."""
    def __init__(self, result):
        self.result = result

    async def process_raw_signal(self, chat_id, text):
        return self.result


@pytest.mark.asyncio
async def test_dispatch_raw_message_runs_close_now_directly_and_skips_n8n(monkeypatch):
    forwarded = []
    direct_calls = []

    async def fake_forward(text, chat_id, webhook_url):
        forwarded.append((text, chat_id, webhook_url))

    async def fake_execute(chat_id, text, direction_hint, mgmt_url, action_api_key, redis_client):
        direct_calls.append((chat_id, text, direction_hint))
        return True

    monkeypatch.setattr("services.router_parser.app.forward_to_n8n", fake_forward)
    monkeypatch.setattr("services.router_parser.app.execute_close_now_directly", fake_execute)

    router = RecordingRouter(None)  # not a recognized signal
    redis = FakeRedis()
    await dispatch_raw_message(
        router=router, redis_client=redis, chat_id="-1003321565807",
        text="XAUUSD SELL TRADE INVALID ❌\n\nClose now",
        n8n_webhook_url="https://n8n.example.com/in",
        mgmt_url="http://trade_orchestrator:8200/mgmt/action", action_api_key="test-key",
    )

    assert direct_calls == [("-1003321565807", "XAUUSD SELL TRADE INVALID ❌\n\nClose now", "SELL")]
    assert forwarded == []


@pytest.mark.asyncio
async def test_dispatch_raw_message_forwards_unrecognized_text_to_n8n(monkeypatch):
    forwarded = []
    direct_calls = []

    async def fake_forward(text, chat_id, webhook_url):
        forwarded.append((text, chat_id, webhook_url))

    async def fake_execute(**kwargs):
        direct_calls.append(kwargs)
        return True

    monkeypatch.setattr("services.router_parser.app.forward_to_n8n", fake_forward)
    monkeypatch.setattr("services.router_parser.app.execute_close_now_directly", fake_execute)

    router = RecordingRouter(None)
    redis = FakeRedis()
    await dispatch_raw_message(
        router=router, redis_client=redis, chat_id="-1",
        text="HIT SL. GET READY FOR RECOVERY",
        n8n_webhook_url="https://n8n.example.com/in",
        mgmt_url="http://trade_orchestrator:8200/mgmt/action", action_api_key="test-key",
    )

    assert direct_calls == []
    assert forwarded == [("HIT SL. GET READY FOR RECOVERY", "-1", "https://n8n.example.com/in")]


@pytest.mark.asyncio
async def test_dispatch_raw_message_skips_close_now_check_for_recognized_signals(monkeypatch):
    """A text that already parses as a signal must never be evaluated as a close_now candidate."""
    forwarded = []
    direct_calls = []

    async def fake_forward(text, chat_id, webhook_url):
        forwarded.append((text, chat_id, webhook_url))

    async def fake_execute(**kwargs):
        direct_calls.append(kwargs)
        return True

    monkeypatch.setattr("services.router_parser.app.forward_to_n8n", fake_forward)
    monkeypatch.setattr("services.router_parser.app.execute_close_now_directly", fake_execute)

    sig = {"symbol": "XAUUSD", "direction": "SELL", "provider_tag": "TRADE_PULSE", "format_tag": "TRADEPULSE"}
    router = RecordingRouter(sig)
    redis = FakeRedis()

    published = []
    async def fake_xadd(r, stream, fields):
        published.append((stream, fields))
    monkeypatch.setattr("services.router_parser.app.xadd", fake_xadd)

    await dispatch_raw_message(
        router=router, redis_client=redis, chat_id="-1",
        text="XAUUSD SELL NOW",
        n8n_webhook_url="https://n8n.example.com/in",
        mgmt_url="http://trade_orchestrator:8200/mgmt/action", action_api_key="test-key",
    )

    assert direct_calls == []
    assert forwarded == []
    assert len(published) == 1


@pytest.mark.asyncio
async def test_dispatch_raw_message_skips_duplicate_signal_without_forwarding_or_direct_call(monkeypatch):
    forwarded = []
    direct_calls = []

    async def fake_forward(text, chat_id, webhook_url):
        forwarded.append((text, chat_id, webhook_url))

    async def fake_execute(**kwargs):
        direct_calls.append(kwargs)
        return True

    monkeypatch.setattr("services.router_parser.app.forward_to_n8n", fake_forward)
    monkeypatch.setattr("services.router_parser.app.execute_close_now_directly", fake_execute)

    router = RecordingRouter(DUPLICATE_SIGNAL)
    redis = FakeRedis()
    await dispatch_raw_message(
        router=router, redis_client=redis, chat_id="-1", text="XAUUSD SELL NOW",
        n8n_webhook_url="https://n8n.example.com/in",
        mgmt_url="http://trade_orchestrator:8200/mgmt/action", action_api_key="test-key",
    )

    assert direct_calls == []
    assert forwarded == []
