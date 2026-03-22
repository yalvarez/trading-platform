import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from services.common.signal_dedup import SignalDeduplicator

import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_deduplication_new_signal():
    """SET NX devuelve True (clave creada) → señal nueva → is_duplicate=False."""
    redis = AsyncMock()
    redis.set.return_value = True  # clave creada: señal nueva
    dedup = SignalDeduplicator(redis, ttl_seconds=120)
    assert await dedup.is_duplicate('chat1', AsyncMock()) is False

@pytest.mark.asyncio
async def test_deduplication_duplicate_signal():
    """SET NX devuelve None (clave ya existía) → señal duplicada → is_duplicate=True."""
    redis = AsyncMock()
    redis.set.return_value = None  # clave ya existía: duplicado
    dedup = SignalDeduplicator(redis, ttl_seconds=120)
    assert await dedup.is_duplicate('chat1', AsyncMock()) is True

@pytest.mark.asyncio
async def test_deduplication_redis_error_passthrough():
    """Si Redis falla, is_duplicate devuelve False para no bloquear trading."""
    redis = AsyncMock()
    redis.set.side_effect = Exception("connection refused")
    dedup = SignalDeduplicator(redis, ttl_seconds=120)
    assert await dedup.is_duplicate('chat1', AsyncMock()) is False
