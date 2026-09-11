import json

from services.trade_orchestrator.app import parse_channel_names_json


def test_parse_channel_names_json_parses_valid_json():
    raw = json.dumps({"-1001234567890": "Oro Premium"})
    assert parse_channel_names_json(raw) == {"-1001234567890": "Oro Premium"}


def test_parse_channel_names_json_returns_empty_dict_for_blank_string():
    assert parse_channel_names_json("") == {}


def test_parse_channel_names_json_returns_empty_dict_and_logs_on_invalid_json():
    assert parse_channel_names_json("{not valid json") == {}
