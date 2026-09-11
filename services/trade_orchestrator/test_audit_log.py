import json
import os
import tempfile

import pytest

from services.trade_orchestrator.audit_log import append_event, mark_dead_letter


def test_append_event_writes_one_json_line():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "audit_log.jsonl")
        envelope = {"event_id": "abc-123", "event_type": "group_opened", "channel": "both",
                    "timestamp": "2026-09-10T14:32:01.123Z", "message": "hi", "payload": {"group_id": 1}}
        append_event(path, envelope)

        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == envelope


def test_append_event_appends_without_truncating():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "audit_log.jsonl")
        append_event(path, {"event_id": "1", "event_type": "a", "channel": "audit",
                             "timestamp": "t", "message": "m", "payload": {}})
        append_event(path, {"event_id": "2", "event_type": "b", "channel": "audit",
                             "timestamp": "t", "message": "m", "payload": {}})

        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event_id"] == "1"
        assert json.loads(lines[1])["event_id"] == "2"


def test_append_event_creates_file_and_parent_dir_if_missing():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "nested", "audit_log.jsonl")
        append_event(path, {"event_id": "1", "event_type": "a", "channel": "audit",
                             "timestamp": "t", "message": "m", "payload": {}})
        assert os.path.exists(path)


def test_mark_dead_letter_updates_matching_line():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "audit_log.jsonl")
        append_event(path, {"event_id": "1", "event_type": "a", "channel": "audit",
                             "timestamp": "t", "message": "m", "payload": {}})
        append_event(path, {"event_id": "2", "event_type": "b", "channel": "audit",
                             "timestamp": "t", "message": "m", "payload": {}})

        found = mark_dead_letter(path, "2")

        assert found is True
        with open(path, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f.readlines()]
        assert lines[0].get("delivery_status") is None
        assert lines[1]["delivery_status"] == "dead_letter"


def test_mark_dead_letter_returns_false_when_event_id_not_found():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "audit_log.jsonl")
        append_event(path, {"event_id": "1", "event_type": "a", "channel": "audit",
                             "timestamp": "t", "message": "m", "payload": {}})

        assert mark_dead_letter(path, "does-not-exist") is False


def test_append_event_raises_on_io_failure():
    """Regression test: append_event must propagate I/O exceptions.
    Uses a path pointing to an existing directory instead of a file."""
    with tempfile.TemporaryDirectory() as d:
        # Create a directory where the file should be
        dir_path = os.path.join(d, "dir_as_file.jsonl")
        os.makedirs(dir_path)

        envelope = {"event_id": "1", "event_type": "a", "channel": "audit",
                    "timestamp": "t", "message": "m", "payload": {}}

        # Should raise IsADirectoryError (or similar) when trying to open a directory as a file
        with pytest.raises((IsADirectoryError, OSError)):
            append_event(dir_path, envelope)
