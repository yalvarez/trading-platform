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
