import json
import subprocess
from pathlib import Path

import pytest


def test_stalled_transfer_is_bounded_and_preserves_ledger(tmp_path, monkeypatch):
    from texopt.monitoring.server_daily_sync import sync_ledger

    local = tmp_path / 'completions.jsonl'
    original = json.dumps(record('old', '2026-10-02')) + '\n'
    local.write_text(original)

    def stalled(command, *, check, timeout):
        assert timeout == 0.1
        assert 'ServerAliveInterval=15' in command
        Path(command[-1]).write_text('partial download')
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(subprocess, 'run', stalled)
    with pytest.raises(subprocess.TimeoutExpired):
        sync_ledger({'output_dir': str(tmp_path),
                     'server_sync': {'transfer_timeout_seconds': 0.1}},
                    'example', Path('/ledger'), '2026-09-25')
    assert local.read_text() == original
    assert list(tmp_path.iterdir()) == [local]


def test_successful_transfer_adds_new_day(tmp_path, monkeypatch):
    from texopt.monitoring.server_daily_sync import sync_ledger

    def download(command, **kwargs):
        Path(command[-1]).write_text(json.dumps(record('new', '2026-10-03')) + '\n')

    monkeypatch.setattr(subprocess, 'run', download)
    records = sync_ledger({'output_dir': str(tmp_path)}, 'example', Path('/ledger'), '2026-09-25')
    assert records == [record('new', '2026-10-03')]


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
