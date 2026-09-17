import os
import pytest

from services.common.env_validator import validate_router_parser, EnvError


def test_validate_router_parser_requires_n8n_action_api_key(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.delenv("N8N_ACTION_API_KEY", raising=False)
    with pytest.raises(EnvError, match="N8N_ACTION_API_KEY"):
        validate_router_parser()


def test_validate_router_parser_passes_with_n8n_action_api_key(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("N8N_ACTION_API_KEY", "some-key")
    validate_router_parser()  # must not raise
