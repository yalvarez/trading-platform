import pytest
from services.common.config_db import ConfigProvider


def test_get_reads_from_environment(monkeypatch):
    monkeypatch.setenv("SOME_TEST_KEY", "hello")
    provider = ConfigProvider()
    assert provider.get("SOME_TEST_KEY") == "hello"


def test_get_returns_default_when_env_var_missing(monkeypatch):
    monkeypatch.delenv("MISSING_TEST_KEY", raising=False)
    provider = ConfigProvider()
    assert provider.get("MISSING_TEST_KEY", "fallback") == "fallback"


def test_set_writes_to_environment(monkeypatch):
    provider = ConfigProvider()
    provider.set("ANOTHER_TEST_KEY", "written")
    import os
    assert os.environ["ANOTHER_TEST_KEY"] == "written"


def test_get_accounts_reads_accounts_json_env(monkeypatch):
    monkeypatch.setenv("ACCOUNTS_JSON", '[{"name": "acct1", "active": true}]')
    provider = ConfigProvider()
    accounts = provider.get_accounts()
    assert accounts == [{"name": "acct1", "active": True}]


def test_config_provider_has_no_psycopg2_dependency():
    import services.common.config_db as mod
    import inspect
    source = inspect.getsource(mod)
    assert "psycopg2" not in source
