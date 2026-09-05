import pytest
from unittest.mock import AsyncMock, MagicMock
from tests.e2e.runner import run_scenario, format_report, SCENARIOS
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
