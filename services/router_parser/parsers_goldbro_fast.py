"""
Gold Brother FAST parser - detects urgent/fast signals
Format: "Compra/Vende ORO/GOLD ahora @2500" with optional price hint
"""


import re
import os
from typing import Optional
from parsers_base import SignalParser, ParseResult


class GoldBroFastParser(SignalParser):
    format_tag = "GB_FAST"
    
    # Must mention ORO/GOLD explicitly
    SYMBOL_PATTERN = re.compile(r'\b(oro|gold|xau)\b', re.IGNORECASE)
    
    # Buy/sell words in Spanish and English
    BUY_PATTERN = re.compile(r'\b(compra|comprar|compren|buy|long|entrada)\b', re.IGNORECASE)
    SELL_PATTERN = re.compile(r'\b(vende|vender|vendan|venta|sell|short|salida)\b', re.IGNORECASE)
    
    # Must indicate urgency
    URGENCY_PATTERN = re.compile(r'\b(ahora|now|ya|inmediato|asap|de\s+nuevo|nuevamente)\b', re.IGNORECASE)
    
    # Optional hint price (3-5 digit number)
    PRICE_PATTERN = re.compile(r'\b(\d{3,5}(?:\.\d{1,2})?)\b')
    
    # Guard: if looks like complete signal, skip (not a FAST)
    COMPLETE_SIGNAL_PATTERN = re.compile(
        r'\b(entry|sl|stop\s*loss|tp1|tp2|tp3|take\s*profit|target|rango)\b',
        re.IGNORECASE
    )
    
    def parse(self, text: str) -> Optional[ParseResult]:
        norm = self.normalize(text)

        # Si contiene 'Risk Price', es Limitless, no parsear aquí
        if "risk price" in norm.lower():
            return None

        # Skip if looks like complete signal with TP/SL
        if self.COMPLETE_SIGNAL_PATTERN.search(norm):
            return None

        # Must have symbol
        symbol_match = self.SYMBOL_PATTERN.search(norm)
        if symbol_match:
            symbol = symbol_match.group(1).upper()
        else:
            symbol = "NO-SYMBOL"

        # Must have direction
        is_buy = self.BUY_PATTERN.search(norm) is not None
        is_sell = self.SELL_PATTERN.search(norm) is not None
        if not (is_buy or is_sell):
            return None

        # Must have urgency indicator
        if not self.URGENCY_PATTERN.search(norm):
            return None

        # Extract optional price hint
        hint = None
        price_match = self.PRICE_PATTERN.search(norm)
        if price_match:
            try:
                v = float(price_match.group(1))
                if 1000 <= v <= 3000:  # Reasonable XAUUSD range
                    hint = v
            except (ValueError, IndexError):
                pass

        # SL temporal configurable
        sl_pips = float(os.getenv("FAST_TEMP_SL_PIPS", "70"))
        sl = None
        if hint is not None:
            if is_buy:
                sl = hint - sl_pips
            elif is_sell:
                sl = hint + sl_pips

        direction = "BUY" if is_buy else "SELL"
        return ParseResult(
            format_tag=self.format_tag,
            provider_tag="GB_FAST",
            symbol="XAUUSD",
            direction=direction,
            is_fast=True,
            hint_price=hint,
            sl=sl,
        )
