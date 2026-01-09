import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.common.signal_dedup import SignalDeduplicator

import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_deduplication_cases():
    redis = AsyncMock()
    dedup = SignalDeduplicator(redis, ttl_seconds=120)
    redis.exists.return_value = 0
    assert await dedup.is_duplicate('chat1', AsyncMock()) is False
    redis.exists.return_value = 1
    assert await dedup.is_duplicate('chat1', AsyncMock()) is True
