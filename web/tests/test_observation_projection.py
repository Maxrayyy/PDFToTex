"""Progress correctness and evidence I/O across refreshes, restarts and readers."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from web.backend.api import create_app
from web.backend.observations import snapshot, read_snapshot
from web.backend.store import DEFAULTS, Store
from web.backend.worker import Worker


@pytest.fixture
def context(tmp_path):
    app = create_app({**DEFAULTS, 'data_root': str(tmp_path), 'min_free_bytes': 0})
    store = app.state.store
    text = b'\\documentclass{article}\\begin{document}Hello\\end{document}'
    upload = store.upload_create({'path': 'main.tex', 'size': len(text)})
    store.upload_path(upload['id']).write_bytes(text)
    store.finish_upload(upload['id'])
    jobs = store.submit({'kind': 'tex_compile', 'uploads': [upload['id']],
                         'main_file': 'main.tex', 'request_id': 'projection-test'})
    job = store.patch(jobs[0]['id'], status='running', pages=2)
    root = store.root / 'jobs' / job['id']
    work = root / 'work/tex'
    work.mkdir(parents=True)
    return app, store, job, root, work / 'recognize.calls.jsonl'


def finish(ident, page=1, **extra):
    return {'event': 'finish', 'call_id': ident, 'page': page,
            'status': 'ok', 'stage': 'recognize',
            'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}, **extra}


def write_calls(path, *events):
    path.write_text(''.join(json.dumps(e) + '\n' for e in events))


def test_multiple_http_readers_do_not_scan_raw_evidence(context, monkeypatch):
    app, store, job, root, calls = context
    write_calls(calls, finish('a'))
    calls.with_suffix('.tex').write_text('% LEXOID_PAGE_COMPLETED: 2/2\n')
    snapshot(store, job)
    original = Path.open

    def guarded(path, *args, **kwargs):
        if root / 'work' in path.parents:
            pytest.fail('HTTP query reopened stage evidence')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', guarded)
    with TestClient(app) as one, TestClient(app) as two:
        for client in (one, two, one):
            response = client.get('/api/jobs/' + job['id'])
            assert response.status_code == 200
            assert response.json()['completed_pages'] == 1
            assert response.json()['usage']['total_tokens'] == 15
            assert response.json()['observation_status'] == 'current'


def test_new_worker_reads_only_appended_bytes(context, monkeypatch):
    _, store, job, root, calls = context
    write_calls(calls, *(finish(str(n)) for n in range(500)))
    snapshot(store, job)
    appended = json.dumps(finish('next', page=2)) + '\n'
    with calls.open('a') as stream:
        stream.write(appended)
    original = Path.open
    read_sizes = []

    class Measured:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def seek(self, *args): return self.stream.seek(*args)
        def read(self, *args):
            result = self.stream.read(*args)
            read_sizes.append(len(result))
            return result

    def measured(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        return Measured(stream) if path == calls else stream

    monkeypatch.setattr(Path, 'open', measured)
    restarted = Store(store.config)
    result = snapshot(restarted, job)
    assert result['usage']['calls'] == 501
    assert result['usage']['total_tokens'] == 7515
    assert sum(read_sizes) <= len(appended.encode()) + 128
    read_sizes.clear()
    snapshot(restarted, job)
    assert read_sizes == []


@pytest.mark.parametrize('replacement', ['truncate', 'rotate', 'regrow'])
def test_replaced_log_removes_obsolete_calls(context, replacement):
    _, store, job, root, calls = context
    write_calls(calls, finish('old-a'), finish('old-b'))
    snapshot(store, job)
    new = [finish('new-a')]
    if replacement == 'rotate':
        calls.rename(calls.with_suffix('.old'))
    if replacement == 'regrow':
        new.extend(finish('new-' + str(n), error_message='longer') for n in range(4))
    write_calls(calls, *new)
    result = snapshot(store, job)
    assert {c['call_id'] for c in result['calls']} == {e['call_id'] for e in new}
    assert result['usage']['calls'] == len(new)


def test_partial_line_duplicate_unknown_usage_and_deleted_evidence(context):
    _, store, job, root, calls = context
    incomplete = json.dumps(finish('b', page=2, usage=None))
    calls.write_text(json.dumps(finish('a')) + '\n' + incomplete[:40])
    assert snapshot(store, job)['usage']['calls'] == 1
    with calls.open('a') as stream:
        stream.write(incomplete[40:] + '\n' + json.dumps(finish('a')) + '\n')
    result = snapshot(store, job)
    assert result['usage']['calls'] == 2
    assert result['usage']['total_tokens'] is None
    assert result['usage']['known_total_tokens'] == 15
    calls.unlink()
    assert snapshot(store, job)['usage']['calls'] == 0


@pytest.mark.parametrize('corrupt', ['{broken', '{"schema": 1}', '{"schema": 1, "files": {"x": {}}}'])
def test_missing_projection_is_pending_and_corruption_can_be_rebuilt(context, corrupt):
    _, store, job, root, calls = context
    write_calls(calls, finish('a'))
    assert read_snapshot(store, job)['observation_status'] == 'pending'
    snapshot(store, job)
    (root / 'observations-index.json').write_text(corrupt)
    (root / 'observations.json').write_text(corrupt)
    assert read_snapshot(store, job)['observation_status'] == 'pending'
    assert snapshot(store, job)['usage']['calls'] == 1
    assert read_snapshot(store, job)['observation_status'] == 'current'


def test_job_state_changes_invalidate_projection_without_reparsing_evidence(context):
    _, store, job, root, calls = context
    write_calls(calls, {'event': 'start', 'call_id': 'a', 'page': 1,
                       'stage': 'recognize', 'started_at': '2099-01-01T00:00:00'})
    assert snapshot(store, job)['page_rows'][0]['status'] == 'running'
    job = store.patch(job['id'], status='failed')
    assert read_snapshot(store, job)['observation_status'] == 'stale'
    assert snapshot(store, job)['page_rows'][0]['status'] != 'running'


def test_worker_publishes_projection_without_a_browser(context, monkeypatch):
    _, store, job, root, calls = context
    write_calls(calls, finish('a'))
    worker = Worker(store)
    monkeypatch.setattr(worker, 'inspect', lambda name: {'Running': True, 'Status': 'running'})
    job = store.patch(job['id'], started_at=10**12, container='pdftotex-web-projection-test')
    worker.tick()
    assert read_snapshot(store, job)['usage']['calls'] == 1
