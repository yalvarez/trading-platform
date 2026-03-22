"""
Signal deduplication using Redis.
Prevents processing the same signal twice within a time window.
"""

import hashlib
from redis import Redis
import logging

log = logging.getLogger("router_parser.dedup")


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
    
    async def is_duplicate(self, chat_id: str, parse_result) -> bool:
        """
        Verifica si la señal ya fue procesada recientemente.
        Usa SET NX (atómico) — elimina la race condition de la versión anterior
        que usaba exists() + setex() en dos llamadas separadas.

        SET NX devuelve True si la clave NO existía (señal nueva) y la crea con TTL.
        Devuelve False si ya existía (señal duplicada).
        Todo en una sola operación de red — 10-20ms en vez de 20-40ms.
        """
        sig = self._signature_from_parse_result(chat_id, parse_result)
        redis_key = f"{self.key_prefix}{sig}"

        try:
            # SET key value EX ttl NX — atómico, sin race condition
            created = await self.redis.set(redis_key, "1", ex=int(self.ttl_seconds), nx=True)
        except Exception as e:
            log.warning("[DEDUP] SET NX falló sig=%s err=%s — asumiendo nueva señal", sig, e)
            return False  # En caso de error, dejar pasar (no bloquear trading)

        if created:
            return False  # Clave creada: señal nueva
        log.debug("[DEDUP] duplicada chat=%s sig=%s", chat_id, sig)
        return True  # Clave ya existía: duplicado
    
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
