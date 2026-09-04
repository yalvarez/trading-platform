import pytest
from tests.e2e.config import load_config, E2EConfig


def test_load_config_reads_all_fields(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("TG_TEST_CHAT_ID", "-1009999999999")
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "abc")
    monkeypatch.setenv("TG_PHONE", "+10000000000")
    monkeypatch.setenv("MT5_HOST", "mt5_acct1")
    monkeypatch.setenv("MT5_PORT", "8001")
    monkeypatch.setenv("N8N_ACTION_API_KEY", "key")
    monkeypatch.setenv("MGMT_API_PORT", "8200")
    monkeypatch.setenv("TRADE_ORCHESTRATOR_HOST", "trade_orchestrator")

    cfg = load_config()

    assert cfg == E2EConfig(
        redis_url="redis://redis:6379/0",
        tg_test_chat_id=-1009999999999,
        tg_api_id="123",
        tg_api_hash="abc",
        tg_phone="+10000000000",
        mt5_host="mt5_acct1",
        mt5_port=8001,
        n8n_action_api_key="key",
        mgmt_api_port=8200,
        trade_orchestrator_host="trade_orchestrator",
    )


def test_load_config_raises_with_all_missing_vars_named(monkeypatch):
    for var in ("REDIS_URL", "TG_TEST_CHAT_ID", "TG_API_ID", "TG_API_HASH",
                "TG_PHONE", "MT5_HOST", "MT5_PORT", "N8N_ACTION_API_KEY",
                "MGMT_API_PORT", "TRADE_ORCHESTRATOR_HOST"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(RuntimeError) as exc:
        load_config()

    assert "REDIS_URL" in str(exc.value)
    assert "TG_TEST_CHAT_ID" in str(exc.value)
