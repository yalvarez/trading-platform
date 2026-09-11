from services.trade_orchestrator.channel_names import resolve_channel_name


def test_resolves_known_chat_id_to_name():
    mapping = {"-1001234567890": "Oro Premium"}
    assert resolve_channel_name("-1001234567890", mapping) == "Oro Premium"


def test_falls_back_to_raw_chat_id_when_unmapped():
    mapping = {"-1001234567890": "Oro Premium"}
    assert resolve_channel_name("-999", mapping) == "-999"


def test_falls_back_to_placeholder_when_chat_id_is_none():
    mapping = {"-1001234567890": "Oro Premium"}
    assert resolve_channel_name(None, mapping) == "N/D"


def test_works_with_empty_mapping():
    assert resolve_channel_name("-1001234567890", {}) == "-1001234567890"
