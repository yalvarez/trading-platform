import os, re, json, logging
from common.config import Settings
from common.redis_streams import redis_client, xadd, xread_loop, Streams
from gb_filters import looks_like_followup
from torofx_filters import looks_like_torofx_management

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"))
log = logging.getLogger("router_parser")

def parse_signal(text: str) -> dict | None:
    """
    Parse MUY básico (placeholder). Tú lo irás refinando por proveedor:
    - detect BUY/SELL + XAUUSD
    - detect range @a-b
    - SL
    - TP1/TP2/TP3
    """
    up = (text or "").upper()

    # símbolo
    symbol = "XAUUSD" if ("XAU" in up or "ORO" in up) else None
    if not symbol:
        return None

    direction = None
    if "COMPRA" in up or "BUY" in up:
        direction = "BUY"
    if "VENDE" in up or "VENDER" in up or "SELL" in up:
        direction = "SELL"

    if not direction:
        return None

    m_range = re.search(r"@\s*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)", text)
    entry_range = None
    if m_range:
        a = float(m_range.group(1)); b = float(m_range.group(2))
        entry_range = (min(a,b), max(a,b))

    m_sl = re.search(r"\bSL\s*[: ]?\s*(\d+(?:\.\d+)?)", up)
    sl = float(m_sl.group(1)) if m_sl else None

    tps = []
    for k in ["TP1","TP2","TP3"]:
        m = re.search(rf"\b{k}\s*[: ]?\s*(\d+(?:\.\d+)?)", up)
        if m:
            tps.append(float(m.group(1)))

    # Requerimos al menos rango o SL para considerarlo “señal formal”
    if entry_range is None and sl is None:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "entry_range": json.dumps(entry_range) if entry_range else "",
        "sl": str(sl) if sl is not None else "",
        "tps": json.dumps(tps),
        "provider_tag": "GB_LONG" if len(tps) >= 3 else "GB_SCALP",
        "fast": "false",
    }

async def main():
    s = Settings.load()
    r = await redis_client(s.redis_url)

    async for msg_id, fields in xread_loop(r, Streams.RAW, last_id="$"):
        text = fields.get("text","")
        chat_id = fields.get("chat_id","")
        # 1) Filtra followups GB que NO deben abrir trades
        if looks_like_followup(text):
            await xadd(r, Streams.MGMT, {"chat_id": chat_id, "text": text, "provider_hint": "GOLD_BROTHERS"})
            log.info("[MGMT] GB follow-up routed (no open).")
            continue

        # 2) TOROFX management
        if looks_like_torofx_management(text):
            await xadd(r, Streams.MGMT, {"chat_id": chat_id, "text": text, "provider_hint": "TOROFX"})
            log.info("[MGMT] TOROFX management routed.")
            continue

        # 3) Señal
        sig = parse_signal(text)
        if sig:
            sig["chat_id"] = chat_id
            sig["raw_text"] = text
            await xadd(r, Streams.SIGNALS, sig)
            log.info(f"[SIGNAL] {sig['provider_tag']} {sig['direction']} {sig['symbol']}")
        else:
            log.info("[DROP] Not recognized.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())