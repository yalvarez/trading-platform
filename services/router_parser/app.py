import os, re, json, logging, uuid
from common.config import Settings
from common.redis_streams import redis_client, xadd, xread_loop, Streams
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
    
    async for msg_id, fields in xread_loop(r, Streams.RAW, last_id="$"):
        text = fields.get("text","")
        chat_id = fields.get("chat_id","")
        log.debug("[RAW] chat=%s text=%s", chat_id, (text or "").strip()[:200])
        
        if looks_like_followup(text):
            await xadd(r, Streams.MGMT, {"chat_id": chat_id, "text": text, "provider_hint": "GOLD_BROTHERS"})
            log.info("[MGMT] GB follow-up")
            continue
        
        if looks_like_torofx_management(text):
            await xadd(r, Streams.MGMT, {"chat_id": chat_id, "text": text, "provider_hint": "TOROFX"})
            log.info("[MGMT] TOROFX")
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

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
