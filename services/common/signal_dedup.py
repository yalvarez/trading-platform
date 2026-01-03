"""
Signal deduplication using Redis.
Prevents processing the same signal twice within a time window.
"""

import hashlib
import time
from typing import Optional
from redis import Redis


class SignalDeduplicator:
    """
    Deduplicates signals based on message signature.
    Uses Redis to store seen signatures with TTL.
    """
    
    def __init__(self, redis_client: Redis, ttl_seconds: float = 120.0, key_prefix: str = "signal_dedup:"):
        """
        Args:
            redis_client: Redis connection
            ttl_seconds: Time-to-live for dedup entries (default 120s)
            key_prefix: Redis key prefix for dedup entries
        """
        self.redis = redis_client
        self.ttl_seconds = ttl_seconds
        self.key_prefix = key_prefix
    
    def _signature_from_parse_result(self, chat_id: str, parse_result) -> str:
        """
        Generate signature from parse result.
        Signature includes: chat_id, provider, symbol, direction, SL, TPs, entry range
        """
        parts = [
            str(chat_id),
            str(getattr(parse_result, "provider_tag", "")),
            str(getattr(parse_result, "symbol", "")),
            str(getattr(parse_result, "direction", "")),
            str(getattr(parse_result, "sl", "")),
            str(sorted(getattr(parse_result, "tps", []) or [])),
            str(getattr(parse_result, "entry_range", "")),
            str(getattr(parse_result, "hint_price", "")),
        ]
        raw = "|".join(parts)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()
    
    def is_duplicate(self, chat_id: str, parse_result) -> bool:
        """
        Check if signal was already seen recently.
        Returns True if duplicate, False if new signal.
        """
        sig = self._signature_from_parse_result(chat_id, parse_result)
        redis_key = f"{self.key_prefix}{sig}"
        
        # Check if exists
        exists = self.redis.exists(redis_key) > 0
        
        if exists:
            return True  # Duplicate
        
        # Mark as seen with TTL
        self.redis.setex(redis_key, int(self.ttl_seconds), "1")
        return False  # New signal
    
    def cleanup(self) -> int:
        """
        Clean up expired entries (optional manual cleanup).
        Returns number of keys deleted.
        """
        keys = self.redis.keys(f"{self.key_prefix}*")
        if not keys:
            return 0
        return self.redis.delete(*keys)
    
    def reset(self):
        """Clear all dedup entries"""
        self.cleanup()
