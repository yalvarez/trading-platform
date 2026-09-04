from services.trade_orchestrator.trade_utils import parse_group_comment


def test_parses_tp1_leg_comment():
    assert parse_group_comment("TM-GRP1-tp1") == (1, "tp1")


def test_parses_runner_leg_comment():
    assert parse_group_comment("TM-GRP42-runner") == (42, "runner")


def test_parses_multi_digit_group_id():
    assert parse_group_comment("TM-GRP1234-tp1") == (1234, "tp1")


def test_rejects_missing_prefix():
    assert parse_group_comment("GRP1-tp1") is None


def test_rejects_unknown_leg():
    assert parse_group_comment("TM-GRP1-scalp") is None


def test_rejects_non_numeric_group_id():
    assert parse_group_comment("TM-GRPabc-tp1") is None


def test_rejects_empty_string():
    assert parse_group_comment("") is None


def test_rejects_unrelated_comment():
    assert parse_group_comment("PartialClose") is None


def test_rejects_none():
    assert parse_group_comment(None) is None
