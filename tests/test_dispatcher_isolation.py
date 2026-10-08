"""Dispatcher survives independent scan/role/round failures and DB contention."""
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from mbox import dispatcher
from mbox.core import Store


@pytest.fixture
def dispatch_env(tmp_path, monkeypatch):
    store = Store(tmp_path / 'dispatch.db')
    for role in ('lead', 'builder', 'debugger', 'guardian'):
        store.add_agent(role)
    for role in ('builder', 'debugger'):
        store.send({'id': 'lead', 'rank': 'lead'}, role, 'work')
    calls, logs = [], []
    def make(role, cfg, home):
        return SimpleNamespace(level='headless', wake=lambda *a, **kw: calls.append(role) or 'started test')
    monkeypatch.setattr(dispatcher.adapters, 'make', make)
    monkeypatch.setattr(dispatcher.adapters, 'tmux_room_exists', lambda _: False)
    monkeypatch.setattr(dispatcher, 'home', lambda: tmp_path)
    yield store, {'builder': {'adapter': 'test'}, 'debugger': {'adapter': 'test'}}, calls, logs
    store.db.close()


def scan_failure(*args):
    raise sqlite3.OperationalError('database is locked')


def stop_after_rounds(monkeypatch, count):
    slept = []
    def sleep(interval):
        slept.append(interval)
        if len(slept) == count:
            raise KeyboardInterrupt
    monkeypatch.setattr(dispatcher.time, 'sleep', sleep)
    return slept


def test_once_scan_failure_still_wakes_roles(dispatch_env, monkeypatch):
    store, roles, calls, logs = dispatch_env
    monkeypatch.setattr(store, 'alert_must_deliveries', scan_failure)
    dispatcher.once(roles, store, log=logs.append)
    assert calls == ['builder', 'debugger']
    assert any('OperationalError: database is locked' in log for log in logs)
    assert store.inbox({'id': 'builder'}, mark=False)[0]['state'] == 'delivered'


@pytest.mark.parametrize('stage', ['make', 'finish', 'unread_count', 'dispatch_messages', 'wake'])
def test_role_failure_does_not_block_next_role(dispatch_env, monkeypatch, stage):
    store, roles, calls, logs = dispatch_env
    real_make = dispatcher.adapters.make
    def fail():
        raise RuntimeError(f'{stage} failed')
    if stage in ('make', 'finish', 'wake'):
        def make(role, cfg, home):
            if role == 'builder' and stage == 'make':
                raise SystemExit('invalid adapter')
            adapter = real_make(role, cfg, home)
            if role == 'builder':
                setattr(adapter, stage, lambda *a, **kw: fail())
            return adapter
        monkeypatch.setattr(dispatcher.adapters, 'make', make)
    else:
        real = getattr(store, stage)
        def method(role, *args, **kwargs):
            if (role.get('id') if isinstance(role, dict) else role) == 'builder':
                fail()
            return real(role, *args, **kwargs)
        monkeypatch.setattr(store, stage, method)
    dispatcher.once(roles, store, log=logs.append)
    assert calls == ['debugger']
    assert any('role:builder' in log and ('RuntimeError' in log or 'SystemExit' in log) for log in logs)


def test_loop_scan_failure_threshold_and_role_progress(dispatch_env, monkeypatch):
    store, roles, calls, logs = dispatch_env
    monkeypatch.setattr(store, 'alert_must_deliveries', scan_failure)
    slept = stop_after_rounds(monkeypatch, 6)
    with pytest.raises(KeyboardInterrupt):
        dispatcher.loop(roles, store, every=0.01, log=logs.append)
    assert calls == ['builder', 'debugger'] * 6
    assert len(slept) == 6
    alerts = store.inbox({'id': 'guardian'}, mark=False)
    assert len(alerts) == 1  # default N=5, once per failure streak
    assert 'alert_must_deliveries' in alerts[0]['body'] and '5 次' in alerts[0]['body']
    assert 'OperationalError' in alerts[0]['body']
    assert any('!!! DISPATCHER' in log for log in logs)


def test_loop_outer_exception_recovery(dispatch_env, monkeypatch):
    store, roles, calls, logs = dispatch_env
    real_once = dispatcher.once
    rounds = []
    def once(*args, **kwargs):
        rounds.append(1)
        if len(rounds) <= 2:
            raise ValueError('round failed')
        return real_once(*args, **kwargs)
    monkeypatch.setattr(dispatcher, 'once', once)
    stop_after_rounds(monkeypatch, 3)
    with pytest.raises(KeyboardInterrupt):
        dispatcher.loop(roles, store, failure_threshold=2, log=logs.append)
    assert calls == ['builder', 'debugger']
    assert any('loop' in log and 'ValueError' in log for log in logs)
    assert len(store.inbox({'id': 'guardian'}, mark=False)) == 1


def test_failure_counts_reset_and_failed_alert_is_isolated(dispatch_env, monkeypatch):
    store, roles, calls, logs = dispatch_env
    tracker = dispatcher.FailureTracker(2)
    real_send = store.send
    def broken_send(*args, **kwargs):
        raise sqlite3.OperationalError('alert send locked')
    monkeypatch.setattr(store, 'alert_must_deliveries', scan_failure)
    dispatcher.once(roles, store, log=logs.append, failures=tracker)
    assert store.inbox({'id': 'guardian'}, mark=False) == []
    monkeypatch.setattr(store, 'send', broken_send)
    dispatcher.once(roles, store, log=logs.append, failures=tracker)
    assert calls == ['builder', 'debugger'] * 2
    assert any('告警寄送失敗' in log and 'OperationalError' in log for log in logs)
    monkeypatch.setattr(store, 'alert_must_deliveries', lambda _: 0)
    dispatcher.once(roles, store, log=logs.append, failures=tracker)
    assert 'alert_must_deliveries' not in tracker.counts
    monkeypatch.setattr(store, 'send', real_send)
    monkeypatch.setattr(store, 'alert_must_deliveries', scan_failure)
    for _ in range(2):
        dispatcher.once(roles, store, log=logs.append, failures=tracker)
    assert len(store.inbox({'id': 'guardian'}, mark=False)) == 1


def test_role_failure_streak_escalates_independently(dispatch_env, monkeypatch):
    store, roles, calls, logs = dispatch_env
    real_make = dispatcher.adapters.make
    def make(role, cfg, home):
        if role == 'builder':
            raise RuntimeError('role failed')
        return real_make(role, cfg, home)
    monkeypatch.setattr(dispatcher.adapters, 'make', make)
    tracker = dispatcher.FailureTracker(2)
    for _ in range(2):
        dispatcher.once(roles, store, log=logs.append, failures=tracker)
    assert calls == ['debugger'] * 2
    alerts = store.inbox({'id': 'guardian'}, mark=False)
    assert len(alerts) == 1 and 'role:builder' in alerts[0]['body']
    assert tracker.counts == {'role:builder': 2}


def test_store_busy_timeout_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv('MBOX_BUSY_TIMEOUT_MS', raising=False)
    real_connect = sqlite3.connect
    timeouts = []
    def connect(*args, **kwargs):
        timeouts.append(kwargs['timeout'])
        return real_connect(*args, **kwargs)
    monkeypatch.setattr(sqlite3, 'connect', connect)
    default = Store(tmp_path / 'default.db')
    assert default.db.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
    monkeypatch.setenv('MBOX_BUSY_TIMEOUT_MS', '7500')
    configured = Store(tmp_path / 'env.db')
    assert configured.db.execute('PRAGMA busy_timeout').fetchone()[0] == 7500
    explicit = Store(tmp_path / 'explicit.db', busy_timeout_ms=1000)
    assert explicit.db.execute('PRAGMA busy_timeout').fetchone()[0] == 1000
    assert timeouts == [5, 7.5, 1]
    with pytest.raises(ValueError, match='nonnegative'):
        Store(tmp_path / 'invalid.db', busy_timeout_ms=-1)
    for store in (default, configured, explicit):
        store.db.close()


def test_store_waits_for_temporary_write_lock(tmp_path):
    store = Store(tmp_path / 'busy.db', busy_timeout_ms=1000)
    blocker = sqlite3.connect(tmp_path / 'busy.db', check_same_thread=False)
    blocker.execute('BEGIN IMMEDIATE')
    released = threading.Event()
    def release():
        blocker.rollback()
        released.set()
    timer = threading.Timer(0.05, release)
    timer.start()
    try:
        store.add_agent('builder')
        assert released.is_set()
        assert store.agents()[0]['id'] == 'builder'
    finally:
        timer.join()
        blocker.close()
        store.db.close()


def test_fallback_survives_db_alert_failure_updates_and_recovers(dispatch_env, monkeypatch, capsys):
    store, roles, calls, logs = dispatch_env
    tracker = dispatcher.FailureTracker(2)
    alert = dispatcher.home() / 'dispatcher.alert'
    monkeypatch.setattr(store, 'alert_must_deliveries', scan_failure)
    sends = []
    def broken_send(*args, **kwargs):
        # Both backup channels exist before the DB notification is attempted.
        assert alert.exists()
        assert '2 次' in alert.read_text()
        assert 'OperationalError' in capsys.readouterr().err
        sends.append(1)
        raise sqlite3.OperationalError('alert DB locked')
    monkeypatch.setattr(store, 'send', broken_send)
    assert dispatcher.once(roles, store, failures=tracker, log=logs.append) is False
    assert not alert.exists()
    assert dispatcher.once(roles, store, failures=tracker, log=logs.append) is False
    text = alert.read_text()
    assert 'alert_must_deliveries' in text and '2 次' in text
    assert 'OperationalError: database is locked' in text
    assert text[:4].isdigit() and 'T' in text.split()[0]  # timestamp
    assert alert.stat().st_mode & 0o777 == 0o600
    assert any('告警寄送失敗' in log for log in logs)
    monkeypatch.setattr(store, 'alert_must_deliveries', lambda _: (_ for _ in ()).throw(ValueError('latest error')))
    assert dispatcher.once(roles, store, failures=tracker, log=logs.append) is False
    assert '3 次' in alert.read_text() and 'ValueError: latest error' in alert.read_text()
    assert 'ValueError: latest error' in capsys.readouterr().err
    assert sends == [1]  # DB notification remains once per failure streak.
    monkeypatch.setattr(store, 'alert_must_deliveries', lambda _: 0)
    assert dispatcher.once(roles, store, failures=tracker, log=logs.append) is True
    assert not alert.exists()
    assert calls == ['builder', 'debugger'] * 4


def test_fallback_waits_for_entire_round_recovery(dispatch_env, monkeypatch):
    store, roles, calls, logs = dispatch_env
    tracker = dispatcher.FailureTracker(1)
    monkeypatch.setattr(store, 'alert_must_deliveries', scan_failure)
    dispatcher.once(roles, store, failures=tracker, log=logs.append)
    alert = dispatcher.home() / 'dispatcher.alert'
    assert alert.exists()  # Successful role processing alone cannot clear a scan failure.
    monkeypatch.setattr(store, 'alert_must_deliveries', lambda _: 0)
    real_make = dispatcher.adapters.make
    def make(role, cfg, home):
        if role == 'builder':
            raise RuntimeError('builder failed')
        return real_make(role, cfg, home)
    monkeypatch.setattr(dispatcher.adapters, 'make', make)
    dispatcher.once(roles, store, failures=tracker, log=logs.append)
    assert alert.exists() and 'role:builder' in alert.read_text()
    monkeypatch.setattr(dispatcher.adapters, 'make', real_make)
    dispatcher.once(roles, store, failures=tracker, log=logs.append)
    assert not alert.exists()


def test_loop_outer_failure_fallback_and_recovery(dispatch_env, monkeypatch):
    store, roles, calls, logs = dispatch_env
    real_once = dispatcher.once
    alert = dispatcher.home() / 'dispatcher.alert'
    rounds = []
    def once(*args, **kwargs):
        rounds.append(1)
        if len(rounds) == 1:
            raise RuntimeError('outer failure')
        assert alert.exists() and 'outer failure' in alert.read_text()
        return real_once(*args, **kwargs)
    monkeypatch.setattr(dispatcher, 'once', once)
    stop_after_rounds(monkeypatch, 2)
    with pytest.raises(KeyboardInterrupt):
        dispatcher.loop(roles, store, failure_threshold=1, log=logs.append)
    assert len(rounds) == 2 and not alert.exists()


def test_fallback_write_failure_still_notifies_and_keeps_running(dispatch_env, monkeypatch, capsys):
    store, roles, calls, logs = dispatch_env
    monkeypatch.setattr(store, 'alert_must_deliveries', scan_failure)
    real_replace = dispatcher.os.replace
    def blocked_replace(src, dst):
        raise PermissionError('file blocked')
    monkeypatch.setattr(dispatcher.os, 'replace', blocked_replace)
    dispatcher.once(roles, store, failures=dispatcher.FailureTracker(1), log=logs.append)
    assert calls == ['builder', 'debugger']
    assert len(store.inbox({'id': 'guardian'}, mark=False)) == 1
    assert 'OperationalError' in capsys.readouterr().err
    assert any('備援告警寫檔失敗' in log and 'PermissionError' in log for log in logs)
    assert list(dispatcher.home().glob('dispatcher.alert.*.tmp')) == []


@pytest.mark.parametrize('outcome', ['success', 'scan_failure', 'outer_failure'])
def test_loop_heartbeat_after_every_round(dispatch_env, monkeypatch, outcome):
    import os
    store, roles, calls, logs = dispatch_env
    path = dispatcher.home() / 'dispatcher.heartbeat'
    if outcome == 'scan_failure':
        monkeypatch.setattr(store, 'alert_must_deliveries', scan_failure)
    elif outcome == 'outer_failure':
        monkeypatch.setattr(dispatcher, 'once', lambda *a, **kw: scan_failure())
    snapshots = []
    def sleep(interval):
        assert path.exists()
        assert path.stat().st_mtime > 1
        text = path.read_text()
        assert 'time=' in text and f'pid={os.getpid()}' in text
        snapshots.append(text)
        os.utime(path, (1, 1))  # Next round must refresh the file's mtime.
        if len(snapshots) == 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(dispatcher.time, 'sleep', sleep)
    with pytest.raises(KeyboardInterrupt):
        dispatcher.loop(roles, store, every=0, log=logs.append)
    assert len(snapshots) == 2
    if outcome != 'outer_failure':
        assert calls == ['builder', 'debugger'] * 2


def test_loop_heartbeat_write_failure_is_isolated(dispatch_env, monkeypatch):
    from pathlib import Path
    store, roles, calls, logs = dispatch_env
    real_write = Path.write_text
    attempts = []
    def write(path, *args, **kwargs):
        if path.name == 'dispatcher.heartbeat':
            attempts.append(1)
            raise PermissionError('heartbeat blocked')
        return real_write(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', write)
    stop_after_rounds(monkeypatch, 2)
    with pytest.raises(KeyboardInterrupt):
        dispatcher.loop(roles, store, log=logs.append)
    assert attempts == [1, 1]
    assert calls == ['builder', 'debugger'] * 2
    assert sum('heartbeat 寫檔失敗' in log and 'PermissionError' in log for log in logs) == 2
