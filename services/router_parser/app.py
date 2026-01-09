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
from parsers_hannah import HannahParser

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log = logging.getLogger("router_parser")



from common.config import FAST_UPDATE_WINDOW_SECONDS

class SignalRouter:
    def __init__(self, redis_client, dedup_ttl=120.0, channels_config=None):
        from parsers_limitless import LimitlessParser
        self.parser_map = {
            'hannah': HannahParser(),
            'goldbro_long': GoldBroLongParser(),
            'goldbro_fast': GoldBroFastParser(),
            'goldbro_scalp': GoldBroScalpParser(),
            'torofx': ToroFxParser(),
            'daily_signal': DailySignalParser(),
            'limitless': LimitlessParser(),
        }
        self.channels_config = channels_config or {}
        self.deduplicator = SignalDeduplicator(redis_client, ttl_seconds=dedup_ttl)
        self.fast_update_window = FAST_UPDATE_WINDOW_SECONDS
        self.redis = redis_client

    def parse_signal(self, text, chat_id=None):
        norm = text.strip()
        parsers = []
        if chat_id and str(chat_id) in self.channels_config:
            parser_names = self.channels_config[str(chat_id)]
            parsers = [self.parser_map[name] for name in parser_names if name in self.parser_map]
        # Si no hay parsers configurados para el canal, usar todos
        if not parsers:
            parsers = list(self.parser_map.values())
        for parser in parsers:
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
        parse_result = self.parse_signal(text, chat_id=chat_id)
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
    import json
    from common.config import CHANNELS_CONFIG_JSON
    s = Settings.load()
    r = await redis_client(s.redis_url)
    try:
        channels_config = json.loads(CHANNELS_CONFIG_JSON)
    except Exception as e:
        log.warning(f"CHANNELS_CONFIG_JSON parse error: {e}")
        channels_config = {}
    router = SignalRouter(r, dedup_ttl=s.dedup_ttl_seconds, channels_config=channels_config)
    group = "router_group"
    consumer = f"consumer_{os.getpid()}"
    await create_consumer_group(r, Streams.RAW, group)

    async for msg_id, fields in xreadgroup_loop(r, Streams.RAW, group, consumer):
        text = fields.get("text", "")
        chat_id = fields.get("chat_id", "")
        log.debug("[RAW] chat=%s text=%s", chat_id, (text or "").strip()[:200])

        try:
            # Si el texto parece gestión TOROFX o contiene 'Stop Loss' y 'Target: open', priorizar ese parser
            if looks_like_torofx_management(text) or ("stop loss" in text.lower() and "target: open" in text.lower()):
                sig = ToroFxParser().parse(text)
                if sig:
                    trace_id = uuid.uuid4().hex[:8]
                    # Convertir todos los valores a tipos compatibles con Redis
                    sig_dict = {}
                    import json
                    for k, v in (sig.__dict__ if hasattr(sig, "__dict__") else sig).items():
                        if isinstance(v, bool):
                            sig_dict[k] = str(v).lower()
                        elif k == "entry_range" and v is not None:
                            try:
                                import json
                                # Si es string tipo '(4502.0, 4500.0)', conviértelo a lista y serializa
                                if isinstance(v, str):
                                    v_clean = v.strip()
                                    if v_clean.startswith('(') and v_clean.endswith(')'):
                                        v_clean = v_clean[1:-1]
                                        parts = [float(x.strip()) for x in v_clean.split(',') if x.strip()]
                                        sig_dict[k] = json.dumps(parts)
                                    else:
                                        # Si es string pero no formato tupla, intenta cargar como JSON
                                        try:
                                            val = json.loads(v)
                                            sig_dict[k] = json.dumps(val)
                                        except Exception:
                                            sig_dict[k] = json.dumps([])
                                elif isinstance(v, (tuple, list)):
                                    sig_dict[k] = json.dumps(list(v))
                                else:
                                    sig_dict[k] = json.dumps([])
                            except Exception:
                                sig_dict[k] = json.dumps([])
                        elif isinstance(v, (list, tuple)):
                            sig_dict[k] = json.dumps(v)
                        elif v is None:
                            continue
                        else:
                            sig_dict[k] = v
                    sig_dict["chat_id"] = chat_id
                    sig_dict["raw_text"] = text
                    sig_dict["trace"] = trace_id
                    await xadd(r, Streams.SIGNALS, sig_dict)
                    log.info(f"[SIGNAL] trace={trace_id} TOROFX {sig_dict.get('direction','')} {sig_dict.get('symbol','')}")
                    await xack(r, Streams.RAW, group, msg_id)
                    continue
                # Si no parsea, lo manda como gestión
                await xadd(r, Streams.MGMT, {"chat_id": chat_id, "text": text, "provider_hint": "TOROFX"})
                log.info("[MGMT] TOROFX")
                await xack(r, Streams.RAW, group, msg_id)
                continue

            if looks_like_followup(text):
                await xadd(r, Streams.MGMT, {"chat_id": chat_id, "text": text, "provider_hint": "GOLD_BROTHERS"})
                log.info("[MGMT] GB follow-up")
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
