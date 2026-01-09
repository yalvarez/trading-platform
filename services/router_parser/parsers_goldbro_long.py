"""
Gold Brother LONG parser - detects longer-term trade signals
Format: ORO/GOLD BUY/SELL Entry: 2500-2505, SL: 2490, TP1: 2515, TP2: 2530
"""

import re
from typing import Optional, Tuple, List
from .parsers_base import SignalParser, ParseResult
import logging

log = logging.getLogger("router_parser")


class GoldBroLongParser(SignalParser):
    format_tag = "GB_LONG"

    # Accept ORO, GOLD, XAU, XAUUSD, XauUsd, etc.
    SYMBOL_PATTERN = re.compile(r'\b(oro|gold|xau(?:usd)?|xauusd)\b', re.IGNORECASE)
    # Accept BUY/COMPRA/COMPRA AHORA/COMPRA YA/COMPRA/COMPRAR
    BUY_PATTERN = re.compile(r'\b(BUY|COMPRA(?:R)?(?:\s+AHORA)?|COMPRA YA)\b', re.IGNORECASE)
    # Accept SELL/VENTA/VENDER AHORA/VENDE AHORA
    SELL_PATTERN = re.compile(r'\b(SELL|VENTA|VENDER(?:\s+AHORA)?|VENDE(?:\s+AHORA)?)\b', re.IGNORECASE)

    # Entry: 2500-2505 or Entry 2500-2505 or @2500-2505
    ENTRY_PATTERN = re.compile(
        r'(?:entry[\s:]*|@)(\d{3,5}(?:\.\d{1,2})?)\s*[-–]\s*(\d{3,5}(?:\.\d{1,2})?)',
        re.IGNORECASE
    )

    # SL: 2490 or Punto de StopLoss: 2490
    SL_PATTERN = re.compile(r'(?:sl|punto de stop ?loss)[\s:]*([\d]{3,5}(?:\.\d{1,2})?)', re.IGNORECASE)

    # TP1/TP2/TP3: 2515, 2530, etc or Toma de Ganancias 1: 2515 or Take Profit 1: 2515
    TP_PATTERN = re.compile(r'(?:tp[1-3]?|toma de ganancias ?[1-3]?|take profit ?[1-3]?)\s*[:\-]?\s*([\d]{3,5}(?:\.\d{1,2})?)', re.IGNORECASE)
    
    def parse(self, text: str) -> Optional[ParseResult]:
        norm = self.normalize(text)
        log.debug("[GB_LONG_PARSE] norm=%r", norm[:400])

        # Si contiene 'Risk Price', es Limitless, no parsear aquí
        if "risk price" in norm.lower():
            log.debug("[GB_LONG_PARSE] Detected 'Risk Price', skipping for LimitlessParser")
            return None

        # Must have symbol
        symbol_match = self.SYMBOL_PATTERN.search(norm)
        has_symbol = bool(symbol_match)
        log.debug("[GB_LONG_PARSE] has_symbol=%s", has_symbol)
        if not has_symbol:
            return None

        # Map alias to standard symbol
        symbol_raw = symbol_match.group(1).lower() if symbol_match else None
        if symbol_raw in ["gold", "oro", "xau", "xauusd"]:
            symbol = "XAUUSD"
        else:
            symbol = symbol_raw.upper() if symbol_raw else "NO-SYMBOL"

        # Must have direction
        is_buy = self.BUY_PATTERN.search(norm) is not None
        is_sell = self.SELL_PATTERN.search(norm) is not None
        log.debug("[GB_LONG_PARSE] is_buy=%s is_sell=%s", is_buy, is_sell)
        if not (is_buy or is_sell):
            return None

        # Must have entry range (allow "Entry: 4471-4468" or shorthand like "@4471-4468")
        entry_match = self.ENTRY_PATTERN.search(norm)
        if not entry_match:
            alt = re.search(r'@?(\d{3,5}(?:\.\d{1,2})?)\s*[-–]\s*(\d{3,5}(?:\.\d{1,2})?)', norm)
            if alt:
                entry_match = alt

        # fallback: single entry like 'Entry Price: 4471' or lines like '@4471'
        single_entry = None
        if not entry_match:
            se = re.search(r'entry(?:\s*price)?[\s:\-]*@?(\d{3,5}(?:\.\d{1,2})?)', norm, re.IGNORECASE)
            if se:
                single_entry = float(se.group(1))
                entry_match = se

        log.debug("[GB_LONG_PARSE] entry_match=%s single_entry=%s", bool(entry_match), single_entry)
        if not entry_match:
            return None

        try:
            entry_min = float(entry_match.group(1))
            entry_max = float(entry_match.group(2)) if entry_match.lastindex and entry_match.lastindex >= 2 and entry_match.group(2) else entry_min
        except (ValueError, IndexError):
            return None

        # Extract SL
        sl = None
        sl_match = self.SL_PATTERN.search(norm)
        if sl_match:
            try:
                sl = float(sl_match.group(1))
            except (ValueError, IndexError):
                pass

        # Extract TPs
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
            provider_tag="GB_LONG",
            symbol=symbol,
            direction=direction,
            entry_range=(entry_min, entry_max),
            sl=sl,
            tps=tps if tps else None,
        )
