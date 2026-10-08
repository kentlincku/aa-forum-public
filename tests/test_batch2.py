"""Batch two contracts: read-only scans, priorities, idle timers, runs and broker."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from mbox.core import Store


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / 'batch2.db')
    for role, rank in [('lead', 'lead'), ('builder', 'worker'), ('guardian', 'worker'), ('owner', 'human')]:
        st.add_agent(role, 'test', rank)
    yield st
    st.db.close()


def test_age_index_and_empty_scan_no_write_tx(store):
    assert tuple(r['name'] for r in store.db.execute('PRAGMA index_info(ix_deliv_age)')) == ('state', 'updated_at')
    sql = []
    store.db.set_trace_callback(sql.append)
    assert store.alert_stale_deliveries() == 0
    store.inbox({'id': 'builder'}, mark=False)
    assert not any(q.startswith('BEGIN') for q in sql)


def test_stale_scan_reads_while_other_connection_holds_write_lock(store):
    other = sqlite3.connect(store.path)
    other.execute('BEGIN IMMEDIATE')
    try:
        assert store.alert_stale_deliveries() == 0
    finally:
        other.rollback()
        other.close()


def test_stale_scan_rechecks_ack_before_alert(store, monkeypatch):
    mid = store.send({'id': 'lead', 'rank': 'lead'}, 'builder', 'work')['id']
    store.inbox({'id': 'builder'})
    store.db.execute('UPDATE deliveries SET updated_at=0')
    original = store._emit_alert
    def emit(row, *args, **kwargs):
        store.ack({'id': 'builder'}, mid, 'done')
        return original(row, *args, **kwargs)
    monkeypatch.setattr(store, '_emit_alert', emit)
    assert store.alert_stale_deliveries() == 0
    assert store.inbox({'id': 'guardian'}, mark=False) == []


def test_priority_defaults_owner_and_task_forced_must(store):
    worker = {'id': 'lead', 'rank': 'lead'}
    mid = store.send(worker, 'builder', 'FYI', priority='digest', priority_reason='default→digest')['id']
    assert store.db.execute('SELECT priority FROM messages WHERE id=?', (mid,)).fetchone()[0] == 'digest'
    owner = store.auth(store.add_agent('owner', 'human', 'human'))
    for sender, kind, expected in [(owner, 'chat', 'sender=owner'), (worker, 'task', 'task')]:
        mid = store.send(sender, 'builder', 'work', kind=kind, priority='digest', priority_reason='wrong')['id']
        row = store.db.execute('SELECT priority,priority_reason FROM messages WHERE id=?', (mid,)).fetchone()
        assert tuple(row) == ('must', expected)


def test_dispatcher_waits_digest_and_piggybacks_on_must(store, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from mbox import dispatcher
    clock = [10000.0]
    monkeypatch.setattr('mbox.core.time.time', lambda: clock[0])
    msg = store.send({'id': 'lead', 'rank': 'lead'}, 'builder', 'FYI', priority='digest', priority_reason='default→digest')['id']
    calls = []
    monkeypatch.setattr(dispatcher, 'home', lambda: tmp_path)
    monkeypatch.setattr(dispatcher.adapters, 'tmux_room_exists', lambda _: False)
    monkeypatch.setattr(dispatcher.adapters, 'make', lambda *args: SimpleNamespace(
        level='headless', wake=lambda *a, **kw: calls.append(kw['preview']) or 'started test'))
    roles = {'builder': {'adapter': 'test'}}
    dispatcher.once(roles, store, log=lambda _: None)
    assert calls == []
    clock[0] += 1800
    dispatcher.once(roles, store, log=lambda _: None)
    assert len(calls) == 1 and 'FYI' in calls[0]
    dispatcher.once(roles, store, log=lambda _: None)
    assert len(calls) == 1
    store.send({'id': 'lead', 'rank': 'lead'}, 'builder', 'urgent')
    dispatcher.once(roles, store, log=lambda _: None)
    assert len(calls) == 2 and 'urgent' in calls[-1] and 'FYI' in calls[-1]


def setup_must_timer(store, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('mbox.core.time.time', lambda: clock[0])
    mid = store.send({'id': 'lead', 'rank': 'lead'}, 'builder', 'urgent',
                     priority_reason='mention=@builder', source_room=2, source_messages=[22])['id']
    store.inbox({'id': 'builder'})
    return mid, clock


def test_must_timer_pauses_busy_and_preserves_metadata(store, monkeypatch):
    mid, clock = setup_must_timer(store, monkeypatch)
    store.observe_role('builder', 'busy')
    clock[0] += 3600
    assert store.alert_must_deliveries() == 0
    store.observe_role('builder', 'idle')
    clock[0] += 120
    store.observe_role('builder', 'busy')
    clock[0] += 3600
    assert store.alert_must_deliveries() == 0
    store.observe_role('builder', 'idle')
    clock[0] += 179
    assert store.alert_must_deliveries() == 0
    clock[0] += 1
    assert store.alert_must_deliveries() == 1
    assert store.alert_must_deliveries() == 0
    text = store.inbox({'id': 'guardian'}, mark=False)[0]['body']
    for term in (f'#{mid}', '群 #2', '[22]', 'priority_reason=mention=@builder', '送達時間=1000',
                 '角色轉 idle 時間=', '告警時間=', '累積 idle 秒數=300'):
        assert term in text


def test_must_scan_dedup_across_connections_and_readonly_candidates(store, monkeypatch):
    mid, clock = setup_must_timer(store, monkeypatch)
    store.observe_role('builder', 'idle')
    clock[0] += 300
    other = Store(store.path)
    barrier = threading.Barrier(2)
    def check(st):
        barrier.wait()
        return st.alert_must_deliveries()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(check, (store,other))) == [0,1]
        sql = []
        store.db.set_trace_callback(sql.append)
        assert store.alert_must_deliveries() == 0
        assert not any(q.startswith('BEGIN') for q in sql)
    finally:
        other.db.close()


def test_must_rechecks_busy_or_ack_and_ignores_digest(store, monkeypatch):
    mid, clock = setup_must_timer(store, monkeypatch)
    store.observe_role('builder', 'idle')
    clock[0] += 300
    original = store._emit_alert
    def busy(row, *args, **kwargs):
        store.observe_role('builder', 'busy')
        return original(row, *args, **kwargs)
    monkeypatch.setattr(store, '_emit_alert', busy)
    assert store.alert_must_deliveries() == 0
    store.ack({'id': 'builder'}, mid, 'done')
    store.send({'id': 'lead', 'rank': 'lead'}, 'builder', 'FYI', priority='digest', priority_reason='default→digest')
    store.inbox({'id': 'builder'})
    store.observe_role('builder', 'idle')
    clock[0] += 3600
    assert store.alert_must_deliveries() == 0


def test_owner_mismatch_alerts_both_owner_sources_and_dedup(store):
    ids = []
    for sender, source_owner in [({'id': 'owner', 'rank': 'human'}, False),
                                 ({'id': 'lead', 'rank': 'lead'}, True)]:
        mid = store.send(sender, 'builder', 'owner message', source_owner=source_owner)['id']
        store.db.execute("UPDATE messages SET priority='digest',priority_reason='broken classifier' WHERE id=?", (mid,))
        ids.append(mid)
    assert store.alert_priority_mismatches() == 2
    assert store.alert_priority_mismatches() == 0
    alerts = store.inbox({'id': 'guardian'}, mark=False)
    assert len(alerts) == 2
    assert all('broken classifier' in row['body'] for row in alerts)
    assert all(any(f'#{mid}' in row['body'] for row in alerts) for mid in ids)


def test_mismatch_scan_readonly_and_concurrent_dedup(store):
    mid = store.send({'id': 'owner', 'rank': 'human'}, 'builder', 'owner')['id']
    store.db.execute("UPDATE messages SET priority='digest' WHERE id=?", (mid,))
    other = Store(store.path)
    barrier = threading.Barrier(2)
    def check(st):
        barrier.wait()
        return st.alert_priority_mismatches()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(check, (store,other))) == [0,1]
        sql = []
        store.db.set_trace_callback(sql.append)
        assert store.alert_priority_mismatches() == 0
        assert not any(q.startswith('BEGIN') for q in sql)
    finally:
        other.db.close()


def test_mismatch_rechecks_corrected_priority(store, monkeypatch):
    mid = store.send({'id': 'owner', 'rank': 'human'}, 'builder', 'owner')['id']
    store.db.execute("UPDATE messages SET priority='digest' WHERE id=?", (mid,))
    real = store._emit_alert
    def emit(row, *args, **kwargs):
        store.db.execute("UPDATE messages SET priority='must' WHERE id=?", (mid,))
        return real(row, *args, **kwargs)
    monkeypatch.setattr(store, '_emit_alert', emit)
    assert store.alert_priority_mismatches() == 0


def test_run_lifecycle_and_handled_count(store, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('mbox.core.time.time', lambda: clock[0])
    ids = [store.send({'id': 'lead', 'rank': 'lead'}, 'builder', str(i))['id'] for i in range(2)]
    store.begin_run('builder', ids)
    store.begin_run('builder')
    assert len(store.recent_runs()) == 1
    store.ack({'id': 'builder'}, ids[0], 'done')
    clock[0] += 10
    store.finish_run('builder', 7, 'engine failed')
    row = store.recent_runs()[0]
    assert (row['role'],row['started'],row['ended'],row['exit'],row['msgs_handled'],row['error']) == ('builder',1000,1010,7,1,'engine failed')


def test_busy_recorded_once_and_scan_error_saved(store, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from mbox import dispatcher
    state = ['busy']
    logs = []
    tracker = dispatcher.FailureTracker(2)
    monkeypatch.setattr(dispatcher, 'home', lambda: tmp_path)
    monkeypatch.setattr(dispatcher.adapters, 'tmux_room_exists', lambda _: False)
    monkeypatch.setattr(dispatcher.adapters, 'make', lambda *a: SimpleNamespace(level='headless', health=lambda: state[0]))
    for _ in range(3):
        dispatcher.once({'builder': {'adapter': 'test'}}, store, failures=tracker, log=logs.append)
    assert len(store.recent_runs()) == 1 and store.recent_runs()[0]['ended'] is None
    assert sum('busy（' in line for line in logs) == 1
    state[0] = 'idle'
    dispatcher.once({'builder': {'adapter': 'test'}}, store, failures=tracker, log=logs.append)
    assert store.recent_runs()[0]['ended'] is not None
    monkeypatch.setattr(store, 'alert_must_deliveries', lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError('SQLITE_BUSY')))
    dispatcher.once({}, store, failures=tracker, log=logs.append)
    assert 'OperationalError: SQLITE_BUSY' in store.recent_runs()[0]['error']


def test_run_errors_retry_when_database_recovers(store, monkeypatch):
    from mbox.dispatcher import FailureTracker
    tracker = FailureTracker(5)
    real = store.record_run_error
    monkeypatch.setattr(store, 'record_run_error', lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError('locked')))
    logs = []
    tracker.failure('scan', sqlite3.OperationalError('SQLITE_BUSY'), store, logs.append)
    assert len(tracker.pending_errors) == 1
    monkeypatch.setattr(store, 'record_run_error', real)
    tracker.flush_errors(store, logs.append)
    assert tracker.pending_errors == []
    assert store.recent_runs()[0]['role'] == 'scan'
    assert store.recent_runs()[0]['error'] == 'OperationalError: SQLITE_BUSY'


def test_headless_runner_records_real_exit(tmp_path, monkeypatch):
    import adapters
    real_popen = adapters.subprocess.Popen
    children = []
    def popen(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(adapters.subprocess, 'Popen', popen)
    adapter = adapters.CommandHeadless('builder', {'workdir': str(tmp_path / 'work'), 'command': '/bin/sh -c "exit 7"'}, tmp_path)
    assert adapter.wake(1, head=1).startswith('started')
    children[0].wait(timeout=5)
    assert adapter.completion_status() == (7, '')


def test_dispatcher_setup_provisions_system_token(tmp_path, monkeypatch):
    from mbox import dispatcher
    monkeypatch.setattr(dispatcher, 'home', lambda: tmp_path)
    store = Store(tmp_path / 'setup.db')
    try:
        dispatcher.setup({'builder': {'rank': 'worker', 'runtime': 'codex'}}, store)
        token = (tmp_path / 'tokens/server').read_text()
        assert store.auth(token) == {'id': 'server', 'runtime': 'system', 'rank': 'human'}
        dispatcher.setup({'builder': {'rank': 'worker', 'runtime': 'codex'}}, store)
        assert (tmp_path / 'tokens/server').read_text() == token
    finally:
        store.db.close()


def test_only_rendered_notifications_marked_delivered(store, monkeypatch, tmp_path):
    from types import SimpleNamespace
    import adapters
    from mbox import dispatcher
    ids = [store.send({'id': 'lead', 'rank': 'lead'}, 'builder', 'x'*1000)['id'] for _ in range(3)]
    monkeypatch.setattr(adapters, 'PREVIEW_LIMIT', 1200)
    monkeypatch.setattr(dispatcher, 'home', lambda: tmp_path)
    monkeypatch.setattr(adapters, 'tmux_room_exists', lambda _: False)
    monkeypatch.setattr(adapters, 'make', lambda *a: SimpleNamespace(level='headless', wake=lambda *a,**kw: 'started test'))
    dispatcher.once({'builder': {'adapter': 'test'}}, store, log=lambda _: None)
    states = {row['id']:row['state'] for row in store.inbox({'id':'builder'}, mark=False)}
    assert [states[mid] for mid in ids] == ['delivered','queued','queued']


def test_manual_role_pauses_must_timer(store, monkeypatch, tmp_path):
    from mbox import dispatcher
    mid, clock = setup_must_timer(store, monkeypatch)
    store.observe_role('builder', 'idle')
    clock[0] += 100
    monkeypatch.setattr(dispatcher, 'home', lambda: tmp_path)
    dispatcher.once({'builder': {'adapter':'manual'}}, store, log=lambda _: None)
    clock[0] += 1000
    assert store.alert_must_deliveries() == 0
