"""
Sends real Telegram messages to a dedicated e2e test channel via Telethon,
using a session separate from the bot's own telegram_ingestor session.
TG_TEST_CHAT_ID must be a channel/group already present in allowed_channels
of ACCOUNTS_JSON — see docs/superpowers/specs/2026-09-04-e2e-test-suite-design.md
section 3.1 for why plain TG_TEST_CHAT_ID alone does not make telegram_ingestor
process the message.
"""
from telethon import TelegramClient


class TelegramSender:
    def __init__(self, api_id: str, api_hash: str, phone: str, session_name: str = "e2e_test_session"):
        self.phone = phone
        self._client = TelegramClient(session_name, api_id, api_hash)
        self._started = False

    async def _ensure_started(self) -> None:
        if not self._started:
            await self._client.start(phone=self.phone)
            self._started = True

    async def send(self, chat_id: int, text: str) -> int:
        await self._ensure_started()
        sent = await self._client.send_message(chat_id, text)
        return sent.id

    async def close(self) -> None:
        if self._started:
            await self._client.disconnect()
            self._started = False
