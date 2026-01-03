Deployment notes

- Remove the deprecated `version` key in `docker-compose.yml` (already done).

Telegram session sharing

- The project stores the Telethon session at `services/telegram_ingestor/telegram_ingestor.session`.
- `telegram_ingestor` mounts this file read-write so the session can be created from the host.
- `trade_orchestrator` mounts it read-only so it can reuse the same Telethon session: container config in `docker-compose.yml` mounts the file to `/app/services/telegram_ingestor/telegram_ingestor.session`.

Environment variables

Ensure the following env vars exist in `.env` or are provided to the environment:
- `TG_API_ID`
- `TG_API_HASH`
- `TG_SOURCE_CHATS` (comma separated list of chat ids)
- `TG_NOTIFY_TARGET` (optional override for the first target chat)

Quick commands

Rebuild/restart services:

```bash
docker compose up -d --build
```

View `trade_orchestrator` logs:

```bash
docker compose logs -f trade_orchestrator
```

Send a test notification from the host (interactive session required):

```bash
python services/trade_orchestrator/test_notify.py
```

Send a test notification from inside the running `trade_orchestrator` container:

```bash
docker compose exec trade_orchestrator sh -c "python - <<'PY'
import asyncio, os
from telethon import TelegramClient
from common.telegram_notifier import TelegramNotifier, NotificationConfig

async def main():
    api_id = int(os.getenv('TG_API_ID'))
    api_hash = os.getenv('TG_API_HASH')
    session_path = 'services/telegram_ingestor/telegram_ingestor'
    async with TelegramClient(session_path, api_id, api_hash) as client:
        configs = [NotificationConfig('ACCT1', int(os.getenv('TG_SOURCE_CHATS').split(',')[0]))]
        notifier = TelegramNotifier(client, configs)
        await notifier.notify_trade_opened('ACCT1', 99999, 'XAUUSD', 'BUY', 2500.5, 2490.0, [2515.0,2530.0], 0.1, 'CONTAINER_TEST')
        print('sent')

asyncio.run(main())
PY"
```
