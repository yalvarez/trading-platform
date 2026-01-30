
import os, re, json, logging, uuid
from common.config import Settings
from common.redis_streams import redis_client, xadd, Streams, create_consumer_group, xreadgroup_loop, xack
from common.signal_dedup import SignalDeduplicator

from .gb_filters import looks_like_followup
from .torofx_filters import looks_like_torofx_management
from .parsers_base import SignalParser, ParseResult

from .parsers_goldbro_fast import GoldBroFastParser
from .parsers_goldbro_long import GoldBroLongParser
from .parsers_goldbro_scalp import GoldBroScalpParser
from .parsers_torofx import ToroFxParser
from .parsers_daily_signal import DailySignalParser
from .parsers_hannah import HannahParser

# Importar el bus centralizado para publicar comandos
from .bus import TradeBus


# Add container label to log format for Grafana filtering
container_label = os.getenv("CONTAINER_LABEL") or os.getenv("HOSTNAME") or "router_parser"
log_fmt = f"%(asctime)s %(levelname)s [{container_label}] %(name)s: %(message)s"
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"), format=log_fmt)
log = logging.getLogger("router_parser")



from common.config import FAST_UPDATE_WINDOW_SECONDS

class SignalRouter:
    def __init__(self, redis_client, dedup_ttl=120.0, channels_config=None):
        from .parsers_limitless import LimitlessParser
        from .parsers_limitless import LimitlessParser
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
        # --- 1. LIMITLESS si tiene 'Risk Price' ---
        norm_lower = norm.lower()
        if 'risk price' in norm_lower:
            from .parsers_limitless import LimitlessParser
            parser = LimitlessParser()
            try:
                result = parser.parse(norm)
                if result:
                    if hasattr(result, 'entry_range') and result.entry_range is not None:
                        try:
                            entry_range = list(map(float, result.entry_range))
                            result = result.__class__(**{**result.__dict__, 'entry_range': entry_range})
                        except Exception as e:
                            log.warning(f"[PARSE_ERROR] entry_range conversion: {e}")
                            result = result.__class__(**{**result.__dict__, 'entry_range': None})
                    # log.debug(f"[PARSE] {parser.format_tag} matched (LIMITLESS priority)")  # Reduce log noise
                    return result
            except Exception as e:
                log.warning(f"[PARSE_ERROR] LimitlessParser: {e}")
            return None
        # --- 2. TOROFX si tiene 'Target: open' ---
        if 'target: open' in norm_lower:
            from .parsers_torofx import ToroFxParser
            parser = ToroFxParser()
            try:
                result = parser.parse(norm)
                if result:
                    if hasattr(result, 'entry_range') and result.entry_range is not None:
                        try:
                            entry_range = list(map(float, result.entry_range))
                            result = result.__class__(**{**result.__dict__, 'entry_range': entry_range})
                        except Exception as e:
                            log.warning(f"[PARSE_ERROR] entry_range conversion: {e}")
                            result = result.__class__(**{**result.__dict__, 'entry_range': None})
                    # log.debug(f"[PARSE] {parser.format_tag} matched (TOROFX priority)")  # Reduce log noise
                    return result
            except Exception as e:
                log.warning(f"[PARSE_ERROR] ToroFxParser: {e}")
            return None
        # --- 3. HANNAH si hace match (prioridad absoluta sobre cualquier otro parser) ---
        from .parsers_hannah import HannahParser
        hannah_parser = HannahParser()
        try:
            result = hannah_parser.parse(norm)
            if result:
                # log.debug(f"[PARSE] {hannah_parser.format_tag} matched (HANNAH priority)")  # Reduce log noise
                return result
        except Exception as e:
            log.warning(f"[PARSE_ERROR] HannahParser: {e}")
        # --- 4. Normal routing ---
        parsers = []
        if chat_id and str(chat_id) in self.channels_config:
            parser_names = self.channels_config[str(chat_id)]
            parsers = [self.parser_map[name] for name in parser_names if name in self.parser_map]
        if not parsers:
            parsers = list(self.parser_map.values())
        for parser in parsers:
            try:
                result = parser.parse(norm)
                if result:
                    if hasattr(result, 'entry_range') and result.entry_range is not None:
                        try:
                            entry_range = list(map(float, result.entry_range))
                            result = result.__class__(**{**result.__dict__, 'entry_range': entry_range})
                        except Exception as e:
                            log.warning(f"[PARSE_ERROR] entry_range conversion: {e}")
                            result = result.__class__(**{**result.__dict__, 'entry_range': None})
                    # log.debug(f"[PARSE] {parser.format_tag} matched")  # Reduce log noise
                    return result
            except Exception as e:
                log.warning(f"[PARSE_ERROR] {parser.__class__.__name__}: {e}")
                continue
        # log.debug("[PARSE] no parser matched")  # Reduce log noise
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
                # log.info(f"[FAST-UPDATE] Actualizando señal FAST previa para {parse_result.symbol} {parse_result.direction}")  # Reduce log noise
                await self.redis.delete(fast_key)
                # No deduplicar, forzar update
            elif await self.deduplicator.is_duplicate(chat_id, parse_result):
                # log.info("[DEDUP] %s", parse_result.provider_tag)  # Reduce log noise
                return None
        else:
            # Es señal FAST, guarda referencia para posible actualización
            key_prefix = f"fast_sig:{chat_id}:{parse_result.symbol}:{parse_result.direction}"
            await self.redis.setex(key_prefix, int(self.fast_update_window), "1")
            if await self.deduplicator.is_duplicate(chat_id, parse_result):
                # log.info("[DEDUP] %s", parse_result.provider_tag)  # Reduce log noise
                return None

        entry_range = json.dumps(parse_result.entry_range) if parse_result.entry_range else ""
        tps = parse_result.tps or []

        # Always ensure entry_range is a valid JSON array (never a string tuple)
        if parse_result.entry_range is not None:
            try:
                entry_range = json.dumps(list(map(float, parse_result.entry_range)))
            except Exception:
                entry_range = json.dumps([])
        else:
            entry_range = json.dumps([])

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
    # Instanciar el bus centralizado para publicar comandos
    bus = TradeBus(s.redis_url)
    await bus.connect()
    group = "router_group"
    consumer = f"consumer_{os.getpid()}"

    # Bucle robusto: reintenta creación de grupo si ocurre NOGROUP
    import asyncio
    while True:
        try:
            async for msg_id, fields in xreadgroup_loop(r, Streams.RAW, group, consumer):
                text = fields.get("text", "")
                chat_id = fields.get("chat_id", "")
                # log.debug("[RAW] chat=%s text=%s", chat_id, (text or "").strip()[:200])  # Reduce log noise

                # Si el texto parece gestión TOROFX o contiene 'Stop Loss' y 'Target: open', priorizar ese parser
                if looks_like_torofx_management(text) or ("stop loss" in text.lower() and "target: open" in text.lower()):
                    sig = ToroFxParser().parse(text)
                    if sig:
                        trace_id = uuid.uuid4().hex[:8]
                        sig_dict = {}
                        # --- SERIALIZACIÓN ROBUSTA DE entry_range ---
                        entry_range_val = sig.entry_range
                        import json
                        if entry_range_val is None:
                            entry_range_json = json.dumps([])
                        elif isinstance(entry_range_val, str):
                            v_clean = entry_range_val.strip()
                            if v_clean.startswith('(') and v_clean.endswith(')'):
                                v_clean = v_clean[1:-1]
                                parts = [float(x.strip()) for x in v_clean.split(',') if x.strip()]
                                entry_range_json = json.dumps(parts)
                            else:
                                try:
                                    val = json.loads(v_clean)
                                    entry_range_json = json.dumps(val)
                                except Exception:
                                    entry_range_json = json.dumps([])
                        elif isinstance(entry_range_val, (tuple, list)):
                            entry_range_json = json.dumps(list(entry_range_val))
                        else:
                            entry_range_json = json.dumps([])
                        for k, v in (sig.__dict__ if hasattr(sig, "__dict__") else sig).items():
                            if k == "entry_range":
                                sig_dict[k] = entry_range_json
                            elif isinstance(v, bool):
                                sig_dict[k] = str(v).lower()
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

                try:
                    sig = await router.process_raw_signal(chat_id, text)
                    if sig:
                        trace_id = uuid.uuid4().hex[:8]
                        sig["chat_id"] = chat_id
                        sig["raw_text"] = text
                        sig["trace"] = trace_id
                        await xadd(r, Streams.SIGNALS, sig)
                        log.info(f"[SIGNAL] trace={trace_id} {sig['provider_tag']} {sig['direction']} {sig['symbol']}")
                        # Publicar solo la señal parseada, sin lógica de cuentas, volumen ni modalidad
                        if sig.get("symbol") and sig.get("direction"):
                            command = {
                                "signal_id": sig.get("trace", trace_id),
                                "type": "open",
                                "symbol": sig.get("symbol"),
                                "direction": sig.get("direction"),
                                "entry_range": sig.get("entry_range"),
                                "sl": sig.get("sl"),
                                "tp": json.loads(sig.get("tps", "[]")),
                                "provider_tag": sig.get("provider_tag"),
                                "timestamp": int(uuid.uuid1().time // 1e7),
                                "chat_id": chat_id,
                                "raw_text": text,
                            }
                            await bus.publish_command(command)
                            log.info(f"[COMMAND] Publicado en trade_commands: {command}")
                    else:
                        pass  # log.debug("[DROP] chat=%s parsed=None", chat_id)  # Reduce log noise
                finally:
                    await xack(r, Streams.RAW, group, msg_id)
        except Exception as e:
            if "NOGROUP" in str(e):
                log.warning("[REDIS] NOGROUP detectado, reintentando creación de grupo...")
                await create_consumer_group(r, Streams.RAW, group)
                await asyncio.sleep(1)
                continue
            else:
                log.error(f"[FATAL] Error inesperado en bucle de consumo: {e}")
                raise

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
