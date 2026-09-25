import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services.telegram_ingestor.app import build_channel_filter


def test_no_accounts_define_filter_means_no_filtering():
    allowed, any_defined = build_channel_filter([{"name": "acct1", "active": True}])
    assert any_defined is False
    assert allowed == set()


def test_empty_accounts_list_means_no_filtering():
    allowed, any_defined = build_channel_filter([])
    assert any_defined is False
    assert allowed == set()


def test_single_account_with_allowed_channels():
    accounts = [{"name": "acct1", "active": True, "allowed_channels": [-1003321565807]}]
    allowed, any_defined = build_channel_filter(accounts)
    assert any_defined is True
    assert allowed == {"-1003321565807"}


def test_union_across_multiple_accounts_active_and_inactive():
    accounts = [
        {"name": "acct1", "active": True, "allowed_channels": [-1003321565807]},
        {"name": "acct2", "active": False, "allowed_channels": [-1002293184715, -1003321565807]},
        {"name": "acct3", "active": True},  # no allowed_channels field at all
    ]
    allowed, any_defined = build_channel_filter(accounts)
    assert any_defined is True
    assert allowed == {"-1003321565807", "-1002293184715"}


# --- Recovery after a host reboot (production 2026-09-07: Redis was not up yet,
# the ingestor failed 3 quick starts, supervisord put it in FATAL, and the
# container stayed "Up" while no Telegram message was received for ~2 hours). ---

import asyncio
import configparser
import pytest


@pytest.mark.asyncio
async def test_connect_redis_with_retry_keeps_trying_until_redis_is_up():
    from services.telegram_ingestor.app import connect_redis_with_retry

    attempts, sleeps = [], []

    async def fake_connect(url):
        attempts.append(url)
        if len(attempts) < 4:
            raise ConnectionError("Connection refused")
        return "redis-ok"

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    r = await connect_redis_with_retry("redis://redis:6379/0", connect=fake_connect, sleep=fake_sleep)

    assert r == "redis-ok"
    assert len(attempts) == 4
    assert sleeps == sorted(sleeps) and all(0 < s <= 30 for s in sleeps)  # growing backoff, capped


@pytest.mark.asyncio
async def test_connect_redis_with_retry_backoff_is_capped():
    from services.telegram_ingestor.app import connect_redis_with_retry

    calls, sleeps = [0], []

    async def fake_connect(url):
        calls[0] += 1
        if calls[0] < 20:
            raise ConnectionError("down")
        return "ok"

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    await connect_redis_with_retry("redis://x", connect=fake_connect, sleep=fake_sleep)
    assert max(sleeps) == 30


def test_supervisord_never_gives_up_on_the_ingestor():
    """With the default startretries=3, three quick failed starts (e.g. Redis or
    Telegram not reachable yet after a reboot) leave the program FATAL forever
    while the container keeps looking healthy."""
    conf = configparser.ConfigParser()
    conf.read(os.path.join(os.path.dirname(__file__), "supervisord.conf"))
    prog = conf["program:ingestor"]
    assert int(prog.get("startretries", "3")) >= 100000
    assert int(prog.get("startsecs", "1")) >= 10
