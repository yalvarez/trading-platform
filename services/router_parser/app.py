import os, re, json, logging, uuid
from common.config import Settings
from common.redis_streams import redis_client, xadd, Streams, create_consumer_group, xreadgroup_loop, xack
from common.signal_dedup import SignalDeduplicator
from gb_filters import looks_like_followup
from torofx_filters import looks_like_torofx_management
from parsers_base import SignalParser, ParseResult
from parsers_goldbro_fast import GoldBroFastParser
from parsers_goldbro_long import GoldBroLongParser
from parsers_goldbro_scalp import GoldBroScalpParser
from parsers_torofx import ToroFxParser
from parsers_daily_signal import DailySignalParser

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log = logging.getLogger("router_parser")



from common.config import FAST_UPDATE_WINDOW_SECONDS

class SignalRouter:
    def __init__(self, redis_client, dedup_ttl=120.0):
        self.parsers = [
            DailySignalParser(),
            ToroFxParser(),
            GoldBroScalpParser(),
            GoldBroLongParser(),
            GoldBroFastParser(),
        ]
        self.deduplicator = SignalDeduplicator(redis_client, ttl_seconds=dedup_ttl)
        # Ventana de actualización para señales FAST (segundos)
        self.fast_update_window = FAST_UPDATE_WINDOW_SECONDS
        self.redis = redis_client

    def parse_signal(self, text):
        norm = text.strip()
        for parser in self.parsers:
            try:
                result = parser.parse(norm)
                if result:
                    log.debug(f"[PARSE] {parser.format_tag} matched")
                    return result
            except Exception as e:
                log.warning(f"[PARSE_ERROR] {parser.__class__.__name__}: {e}")
                continue
        log.debug("[PARSE] no parser matched")
        return None

    async def process_raw_signal(self, chat_id, text):
        parse_result = self.parse_signal(text)
        if not parse_result:
            return None

        # Si es señal completa, busca una FAST previa para actualizar
        if not parse_result.is_fast:
            # Buscar señales FAST recientes para el mismo chat, símbolo y dirección
            key_prefix = f"fast_sig:{chat_id}:{parse_result.symbol}:{parse_result.direction}"
            fast_key = f"{key_prefix}"
            fast_data = await self.redis.get(fast_key)
            if fast_data:
                # Hay una señal FAST previa, actualizarla
                log.info(f"[FAST-UPDATE] Actualizando señal FAST previa para {parse_result.symbol} {parse_result.direction}")
                await self.redis.delete(fast_key)
                # No deduplicar, forzar update
            elif await self.deduplicator.is_duplicate(chat_id, parse_result):
                log.info("[DEDUP] %s", parse_result.provider_tag)
                return None
        else:
            # Es señal FAST, guarda referencia para posible actualización
            key_prefix = f"fast_sig:{chat_id}:{parse_result.symbol}:{parse_result.direction}"
            await self.redis.setex(key_prefix, int(self.fast_update_window), "1")
            if await self.deduplicator.is_duplicate(chat_id, parse_result):
                log.info("[DEDUP] %s", parse_result.provider_tag)
                return None

        entry_range = json.dumps(parse_result.entry_range) if parse_result.entry_range else ""
        tps = parse_result.tps or []

        return {
            "symbol": parse_result.symbol,
            "direction": parse_result.direction,
            "entry_range": entry_range,
            "sl": str(parse_result.sl) if parse_result.sl is not None else "",
            "tps": json.dumps(tps),
            "provider_tag": parse_result.provider_tag,
            "format_tag": parse_result.format_tag,
            "fast": "true" if parse_result.is_fast else "false",
            "hint_price": str(parse_result.hint_price) if parse_result.hint_price else "",
        }

async def main():
    s = Settings.load()
    r = await redis_client(s.redis_url)
    router = SignalRouter(r, dedup_ttl=s.dedup_ttl_seconds)
    group = "router_group"
    consumer = f"consumer_{os.getpid()}"
    await create_consumer_group(r, Streams.RAW, group)

    async for msg_id, fields in xreadgroup_loop(r, Streams.RAW, group, consumer):
        text = fields.get("text", "")
        chat_id = fields.get("chat_id", "")
        log.debug("[RAW] chat=%s text=%s", chat_id, (text or "").strip()[:200])

        try:
            if looks_like_followup(text):
                await xadd(r, Streams.MGMT, {"chat_id": chat_id, "text": text, "provider_hint": "GOLD_BROTHERS"})
                log.info("[MGMT] GB follow-up")
                await xack(r, Streams.RAW, group, msg_id)
                continue

            if looks_like_torofx_management(text):
                await xadd(r, Streams.MGMT, {"chat_id": chat_id, "text": text, "provider_hint": "TOROFX"})
                log.info("[MGMT] TOROFX")
                await xack(r, Streams.RAW, group, msg_id)
                continue

            sig = await router.process_raw_signal(chat_id, text)
            if sig:
                trace_id = uuid.uuid4().hex[:8]
                sig["chat_id"] = chat_id
                sig["raw_text"] = text
                sig["trace"] = trace_id
                await xadd(r, Streams.SIGNALS, sig)
                log.info(f"[SIGNAL] trace={trace_id} {sig['provider_tag']} {sig['direction']} {sig['symbol']}")
            else:
                log.debug("[DROP] chat=%s parsed=None", chat_id)
        finally:
            await xack(r, Streams.RAW, group, msg_id)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
