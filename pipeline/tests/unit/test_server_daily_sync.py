import json

import pytest


def record(identifier, date):
    return {"id": identifier, "date": date}


def test_server_records_replace_local_records_from_cutover_date():
    from texopt.monitoring.server_daily_sync import merge_records

    local = [
        record("local-24", "2026-09-24"),
        record("local-25", "2026-09-25"),
    ]
    remote = [
        record("remote-24", "2026-09-24"),
        record("remote-25", "2026-09-25"),
        record("remote-26", "2026-09-26"),
    ]

    assert merge_records(local, remote, "2026-09-25") == [
        record("local-24", "2026-09-24"),
        record("remote-25", "2026-09-25"),
        record("remote-26", "2026-09-26"),
    ]


def test_invalid_remote_ledger_does_not_replace_local_ledger(tmp_path):
    from texopt.monitoring.server_daily_sync import merge_ledger

    local = tmp_path / "completions.jsonl"
    remote = tmp_path / "server.jsonl"
    original = json.dumps(record("local-24", "2026-09-24")) + "\n"
    local.write_text(original)
    remote.write_text('{"id":"broken"}\n')

    with pytest.raises(ValueError, match="date"):
        merge_ledger(local, remote, "2026-09-25")
    assert local.read_text() == original


def test_duplicate_remote_ids_are_rejected(tmp_path):
    from texopt.monitoring.server_daily_sync import merge_ledger

    local = tmp_path / "completions.jsonl"
    remote = tmp_path / "server.jsonl"
    local.write_text("")
    duplicate = json.dumps(record("same", "2026-09-25"))
    remote.write_text(duplicate + "\n" + duplicate + "\n")

    with pytest.raises(ValueError, match="duplicate id"):
        merge_ledger(local, remote, "2026-09-25")
