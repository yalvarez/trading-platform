import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from services.common.signal_dedup import SignalDeduplicator

import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_deduplication():
    redis = AsyncMock()
    dedup = SignalDeduplicator(redis, ttl_seconds=120)
    # Simula que la señal nunca ha sido vista
    redis.exists.return_value = 0
    result1 = await dedup.is_duplicate('chat1', AsyncMock())
    assert result1 is False
    # Simula que la señal ya fue vista
    redis.exists.return_value = 1
    result2 = await dedup.is_duplicate('chat1', AsyncMock())
    assert result2 is True
