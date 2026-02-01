import os, sys, json, asyncio
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from telethon import TelegramClient
from services.common.telegram_notifier import TelegramNotifier, NotificationConfig

async def main():
    # derive a target chat id from env
    chat_list = os.getenv('TG_NOTIFY_TARGET') or os.getenv('TG_SOURCE_CHATS') or ''
    if not chat_list:
        # try to read .env file in repo root
        env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))
        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip().startswith('TG_SOURCE_CHATS='):
                        val = line.split('=', 1)[1].strip()
                        chat_list = val
                        break
        if not chat_list:
            print('No TG_SOURCE_CHATS or TG_NOTIFY_TARGET configured in .env')
            return
    try:
        first_chat = int(chat_list.split(',')[0].strip())
    except Exception as e:
        print('Failed to parse chat id:', e)
        return

    configs = [NotificationConfig(account_name='ACCT1', chat_id=first_chat)]

    # Load API credentials (try env, then .env)
    api_id = os.getenv('TG_API_ID')
    api_hash = os.getenv('TG_API_HASH')
    if not api_id or not api_hash:
        env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))
        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip().startswith('TG_API_ID=') and not api_id:
                        api_id = line.split('=',1)[1].strip()
                    if line.strip().startswith('TG_API_HASH=') and not api_hash:
                        api_hash = line.split('=',1)[1].strip()

    if not api_id or not api_hash:
        print('Missing TG_API_ID/TG_API_HASH in env or .env')
        return

    api_id = int(api_id)

    async with TelegramClient('test_notify', api_id, api_hash) as client:
        notifier = TelegramNotifier(client, configs)

        print('Sending test trade_opened notification to', first_chat)
        await notifier.notify_trade_opened(
            account_name='ACCT1',
            ticket=99999,
            symbol='XAUUSD',
            direction='BUY',
            entry_price=2500.5,
            sl_price=2490.0,
            tp_prices=[2515.0, 2530.0],
            lot=0.1,
            provider='TEST'
        )
        print('Notification sent (or attempted)')

if __name__ == '__main__':
    asyncio.run(main())
