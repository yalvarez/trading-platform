import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.telegram_sender import TelegramSender


@pytest.mark.asyncio
async def test_send_calls_telethon_send_message_and_returns_id(monkeypatch):
    fake_sent_message = MagicMock(id=4242)
    fake_client = MagicMock()
    fake_client.start = AsyncMock()
    fake_client.send_message = AsyncMock(return_value=fake_sent_message)
    fake_client.disconnect = AsyncMock()

    monkeypatch.setattr(
        "tests.e2e.telegram_sender.TelegramClient",
        lambda session, api_id, api_hash: fake_client,
    )

    sender = TelegramSender(api_id="1", api_hash="h", phone="+1000000000")
    msg_id = await sender.send(chat_id=-100123, text="XAUUSD BUY NOW")

    assert msg_id == 4242
    fake_client.send_message.assert_awaited_once_with(-100123, "XAUUSD BUY NOW")
    await sender.close()
    fake_client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_is_noop_if_never_started(monkeypatch):
    fake_client = MagicMock()
    fake_client.disconnect = AsyncMock()
    monkeypatch.setattr(
        "tests.e2e.telegram_sender.TelegramClient",
        lambda session, api_id, api_hash: fake_client,
    )
    sender = TelegramSender(api_id="1", api_hash="h", phone="+1000000000")
    await sender.close()
    fake_client.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_twice_only_starts_once(monkeypatch):
    fake_client = MagicMock()
    fake_client.start = AsyncMock()
    fake_client.send_message = AsyncMock(return_value=MagicMock(id=1))
    monkeypatch.setattr(
        "tests.e2e.telegram_sender.TelegramClient",
        lambda session, api_id, api_hash: fake_client,
    )
    sender = TelegramSender(api_id="1", api_hash="h", phone="+1000000000")
    await sender.send(chat_id=-100, text="a")
    await sender.send(chat_id=-100, text="b")
    fake_client.start.assert_awaited_once()
