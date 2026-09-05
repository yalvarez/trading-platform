import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.runner import run_scenario, format_report, SCENARIOS, _build_context, TELEGRAM_SESSION_PATH
from tests.e2e.config import E2EConfig
from tests.e2e.scenarios.base import ScenarioContext, ScenarioOutcome, ScenarioResult


def test_scenarios_registry_has_all_17_entries():
    expected = {
        "a1_fast_only", "a2_fast_then_full_early", "a3_fast_then_full_late", "a4_full_only",
        "b1_be_variant1", "b2_be_variant2", "b3_be_variant3", "b4_forced_close",
        "b5_signal_correction", "b6_milestone_noop", "b7_sl_hit_note", "b8_spam_noop",
        "c1_dedup", "c1b_reopen_after_cooldown", "c2_unrecognized_to_n8n", "c3_entry_range_dash_variants",
        "d1_restart_reconciliation",
    }
    assert set(SCENARIOS.keys()) == expected


@pytest.mark.asyncio
async def test_run_scenario_dispatches_to_registered_function(monkeypatch):
    fake_result = ScenarioResult(name="a1_fast_only", outcome=ScenarioOutcome.PASS, detail="ok")

    async def fake_run(ctx):
        return fake_result

    monkeypatch.setitem(SCENARIOS, "a1_fast_only", fake_run)
    ctx = ScenarioContext(cfg=MagicMock(), price_reader=MagicMock(), sender=MagicMock(), observer=MagicMock())

    result = await run_scenario("a1_fast_only", ctx)

    assert result is fake_result


@pytest.mark.asyncio
async def test_build_context_wires_absolute_telegram_session_path(monkeypatch):
    # TelegramSender's default session_name ("e2e_test_session") is a relative
    # path that Telethon resolves against the process cwd (/app, per the
    # Dockerfile's WORKDIR) — but docker-compose.yml mounts the host session
    # file at /app/tests/e2e/e2e_test_session.session. _build_context must
    # override the default with the absolute path so the session persists
    # across --rm container runs.
    captured = {}

    class FakeTelegramSender:
        def __init__(self, api_id, api_hash, phone, session_name="e2e_test_session"):
            captured["session_name"] = session_name

    monkeypatch.setattr("tests.e2e.runner.TelegramSender", FakeTelegramSender)
    monkeypatch.setattr("tests.e2e.runner.PriceReader", MagicMock())
    monkeypatch.setattr("tests.e2e.runner.VpsObserver", MagicMock())

    fake_redis_asyncio = MagicMock()
    fake_redis_asyncio.from_url = MagicMock(return_value=MagicMock())
    monkeypatch.setitem(
        __import__("sys").modules, "redis.asyncio", fake_redis_asyncio
    )

    cfg = E2EConfig(
        redis_url="redis://localhost:6379/0",
        tg_test_chat_id=-1009999999999,
        tg_api_id="123",
        tg_api_hash="abc",
        tg_phone="+10000000000",
        mt5_host="mt5_acct1",
        mt5_port=8001,
        n8n_action_api_key="key",
        mgmt_api_port=9000,
        trade_orchestrator_host="atp-trade-orchestrator",
    )

    await _build_context(cfg)

    assert captured["session_name"] == "/app/tests/e2e/e2e_test_session"
    assert captured["session_name"] == TELEGRAM_SESSION_PATH
    assert not captured["session_name"].endswith(".session")


def test_format_report_groups_by_outcome():
    results = [
        ScenarioResult(name="a1_fast_only", outcome=ScenarioOutcome.PASS, detail="ok"),
        ScenarioResult(name="b8_spam_noop", outcome=ScenarioOutcome.FAIL, detail="broke"),
        ScenarioResult(name="a4_full_only", outcome=ScenarioOutcome.INCONCLUSIVE_TP1_NOT_REACHED, detail="market quiet"),
    ]

    report = format_report(results)

    assert "PASS" in report and "a1_fast_only" in report
    assert "FAIL" in report and "b8_spam_noop" in report
    assert "INCONCLUSIVE" in report and "a4_full_only" in report
