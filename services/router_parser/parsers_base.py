"""
Base parser framework for trading signal detection.
Supports multiple signal formats: GB_FAST, GB_LONG, GB_SCALP, DAILY_SIGNAL, TOROFX
"""

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class ParseResult:
    """Standardized signal parsing result"""
    format_tag: str              # e.g., GB_FAST, GB_LONG, GB_SCALP, DAILY_SIGNAL, TOROFX
    provider_tag: str            # Identifier for signal provider/strategy
    symbol: str                  # Trading symbol (e.g., XAUUSD)
    direction: str               # BUY or SELL
    
    # Basic info
    is_fast: bool = False        # True for fast/urgent signals
    hint_price: Optional[float] = None
    
    # Complete signal info
    entry_range: Optional[Tuple[float, float]] = None  # (min, max) entry price
    sl: Optional[float] = None   # Stop loss price
    tps: Optional[list[float]] = None  # Take profit levels [tp1, tp2, tp3, ...]
    
    # Addon/extra entries
    addon_prices: Optional[list[float]] = None  # Additional entry levels


class SignalParser:
    """Base class for signal parsers"""
    format_tag: str
    
    def parse(self, text: str) -> Optional[ParseResult]:
        """Parse text and return ParseResult or None if not recognized"""
        raise NotImplementedError
    
    def normalize(self, text: str) -> str:
        """Normalize text for parsing"""
        return (text or "").strip()
