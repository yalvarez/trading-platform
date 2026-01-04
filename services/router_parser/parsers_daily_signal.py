"""
Daily Signal parser - detects daily signal format with MARKET indicator
Format: GOLD MARKET BUY Entry: 2500, SL: 2490, TP1: 2515, TP2: 2530, TP3: 2550
"""

import re
from typing import Optional
from parsers_base import SignalParser, ParseResult
import logging

log = logging.getLogger("router_parser")


class DailySignalParser(SignalParser):
    format_tag = "DAILY_SIGNAL"


    # Detect MARKET or AHORA
    MARKET_PATTERN = re.compile(r'\b(MARKET|AHORA)\b', re.IGNORECASE)
    BUY_PATTERN = re.compile(r'\b(BUY|COMPRA)\b', re.IGNORECASE)
    SELL_PATTERN = re.compile(r'\b(SELL|VENTA)\b', re.IGNORECASE)

    # Símbolo: palabra mayúscula tras BUY/SELL MARKET
    SYMBOL_EXTRACT_PATTERN = re.compile(r'(?:BUY|SELL)\s+MARKET\s+([A-Z]{3,10})', re.IGNORECASE)
    # General: detecta símbolos tipo BTCUSD, XAUUSD, EURUSD, etc.
    SYMBOL_PATTERN = re.compile(r'\b([A-Z]{3,10}|BTCUSD|ETHUSD|XAUUSD|XAGUSD|EURUSD|GBPUSD|USDJPY|USDCAD|AUDUSD|NZDUSD)\b', re.IGNORECASE)

    # Entry: Entry Price: 2500, Entry: 2500-2505, @2500-2505
    ENTRY_PATTERN = re.compile(
        r'(?:entry(?:\s*price)?)[\s:]*([\d]{3,5}(?:\.\d{1,2})?)\s*[-–]?\s*([\d]{3,5}(?:\.\d{1,2})?)?',
        re.IGNORECASE
    )

    # SL: 2490 (con o sin emoji)
    SL_PATTERN = re.compile(r'sl[\s:]*(\d{3,5}(?:\.\d{1,2})?)', re.IGNORECASE)

    # TP1/TP2/TP3: 2515, 2530, 2550 (con o sin emoji)
    TP_PATTERN = re.compile(r'tp[1-3]?[\s:]*(\d{3,5}(?:\.\d{1,2})?)', re.IGNORECASE)

    def parse(self, text: str) -> Optional[ParseResult]:
        norm = self.normalize(text)
        log.debug("[DAILY_PARSE] norm=%r", norm[:400])


        # Extraer símbolo: si hay 'BUY MARKET XXX' o 'SELL MARKET XXX', tomar la palabra después de MARKET
        symbol = None
        market_symbol = re.search(r'(?:BUY|SELL)\s+MARKET\s+([A-Z]{3,10})', norm.upper())
        if market_symbol:
            symbol = market_symbol.group(1).upper()
            log.debug(f"[DAILY_PARSE] market_symbol detectado: {symbol}")
        else:
            symbol_match = self.SYMBOL_EXTRACT_PATTERN.search(norm)
            if symbol_match:
                symbol = symbol_match.group(1).upper()
                log.debug(f"[DAILY_PARSE] symbol_match detectado: {symbol}")
        if not symbol:
            symbol = "NO-SYMBOL"
        log.debug(f"[DAILY_PARSE] symbol final: {symbol}")

        # Debe tener MARKET
        has_market = bool(self.MARKET_PATTERN.search(norm))
        log.debug("[DAILY_PARSE] has_market=%s", has_market)
        if not has_market:
            return None

        # Dirección
        is_buy = self.BUY_PATTERN.search(norm) is not None
        is_sell = self.SELL_PATTERN.search(norm) is not None
        if not (is_buy or is_sell):
            return None

        # Entrada
        entry_match = self.ENTRY_PATTERN.search(norm)
        if not entry_match:
            # Alternativa: @1234-1220 o solo 1234-1220
            alt = re.search(r'@?([\d]{3,5}(?:\.\d{1,2})?)\s*[-–]\s*([\d]{3,5}(?:\.\d{1,2})?)', norm)
            if alt:
                entry_match = alt

        # fallback: single entry como '@4471'
        single_entry = None
        if not entry_match:
            se = re.search(r'entry(?:\s*price)?[\s:\-]*@?([\d]{3,5}(?:\.\d{1,2})?)', norm, re.IGNORECASE)
            if se:
                single_entry = float(se.group(1))
                entry_match = se

        log.debug("[DAILY_PARSE] entry_match=%s single_entry=%s", bool(entry_match), single_entry)
        if not entry_match:
            return None

        try:
            entry_min = float(entry_match.group(1))
            entry_max = float(entry_match.group(2)) if entry_match.lastindex and entry_match.lastindex >= 2 and entry_match.group(2) else entry_min
        except (ValueError, IndexError):
            log.debug("[DAILY_PARSE] entry parse failed groups=%s", entry_match.groups() if entry_match else None)
            return None

        # SL
        sl = None
        sl_match = self.SL_PATTERN.search(norm)
        if sl_match:
            try:
                sl = float(sl_match.group(1))
            except (ValueError, IndexError):
                pass

        # TPs
        tps = []
        for tp_match in self.TP_PATTERN.finditer(norm):
            try:
                tp = float(tp_match.group(1))
                if tp not in tps:
                    tps.append(tp)
            except (ValueError, IndexError):
                pass

        direction = "BUY" if is_buy else "SELL"
        return ParseResult(
            format_tag=self.format_tag,
            provider_tag="DAILY_SIGNAL",
            symbol=symbol,
            direction=direction,
            entry_range=(entry_min, entry_max),
            sl=sl,
            tps=tps if tps else None,
        )
    
