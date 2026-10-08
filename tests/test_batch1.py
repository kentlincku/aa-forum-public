"""Short command environments and persistent stale-delivery alerts."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import adapters
from mbox.core import Store
from mbox.dispatcher import once

ROOT = Path(__file__).resolve().parent.parent


def test_zk_wrapper_http_and_stdin(tmp_path):
    # Isolated installation exercises the real shell wrapper and real CLI.
    (tmp_path / 'bin').mkdir()
    (tmp_path / '.venv/bin').mkdir(parents=True)
    (tmp_path / '.venv/bin/python').symlink_to(sys.executable)
    shutil.copytree(ROOT / 'server', tmp_path / 'server')
    shutil.copy2(ROOT / 'bin/aaf-chat', tmp_path / 'bin/aaf-chat')
    shutil.copy2(ROOT / 'bin/_instance_env.sh', tmp_path / 'bin/_instance_env.sh')
    state = tmp_path / 'custom state'
    state.mkdir()
    (state / 'credentials.json').write_text(json.dumps({'builder': 'test-token'}))
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, self.headers['Authorization'],
                             json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = dict(os.environ, AAF_AGENT='builder', AAF_CHAT_URL=f'http://127.0.0.1:{server.server_port}',
               AAF_SERVER_STATE=str(state), MBOX_AGENT='wrong-role')
    try:
        result = subprocess.run([str(tmp_path / 'bin/aaf-chat'), 'post', '2', '--reply', '10', '--file', '-'],
                                input='第一行\nsecond line', text=True, capture_output=True, env=env)
        assert result.returncode == 0, result.stderr
        assert requests[0][0:2] == ('/api/rooms/2/messages', 'Bearer test-token')
        assert requests[0][2]['body'] == '第一行\nsecond line'
        assert requests[0][2]['reply_to'] == 10
        env.pop('AAF_AGENT')
        env['MBOX_AGENT'] = 'builder'
        result = subprocess.run([str(tmp_path / 'bin/aaf-chat'), 'confirm-read', '2', '10', '11'],
                                capture_output=True, env=env)
        assert result.returncode == 0, result.stderr
        assert requests[1][2] == {'messages': [10, 11]}
        env.pop('MBOX_AGENT')
        result = subprocess.run([str(tmp_path / 'bin/aaf-chat'), '--help'], capture_output=True, env=env)
        assert result.returncode == 2 and b'AAF_AGENT' in result.stderr
    finally:
        server.shutdown()
        server.server_close()


def test_adapter_role_environment_and_tmux(tmp_path, monkeypatch):
    monkeypatch.setenv('AAF_AGENT', 'other-role')
    monkeypatch.setenv('AAF_CHAT_URL', 'http://localhost:9999')
    monkeypatch.setenv('AAF_SERVER_STATE', str(tmp_path / 'state'))
    cfg = {'adapter': 'tmux', 'workdir': str(tmp_path / 'work')}
    adapter = adapters.make('builder', cfg, tmp_path)
    env = adapter.env()
    assert env['AAF_AGENT'] == 'builder'
    assert env['AAF_CHAT_URL'] == 'http://localhost:9999'
    assert env['AAF_SERVER_STATE'] == str(tmp_path / 'state')
    assert env['PATH'].split(':')[0] == str(ROOT / 'bin')
    calls = []
    def tmux(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, '', '')
    monkeypatch.setattr(adapter, '_tmux', tmux)
    adapter.start()
    assert 'AAF_AGENT=builder' in calls[-1]
    assert f'AAF_SERVER_STATE={tmp_path / "state"}' in calls[-1]
    assert 'AAF_CHAT_URL=http://localhost:9999' in calls[-1]
    configured = adapters.make('reviewer', dict(cfg, chat_url='http://localhost:8888',
                                          server_state='/custom/state'), tmp_path).env()
    assert configured['AAF_AGENT'] == 'reviewer'
    assert configured['AAF_CHAT_URL'] == 'http://localhost:8888'
    assert configured['AAF_SERVER_STATE'] == '/custom/state'


def test_stale_delivery_threshold_states_restart_and_no_recursion(tmp_path, monkeypatch):
    monkeypatch.setattr('mbox.core.time.time', lambda: 10000)
    store = Store(tmp_path / 'mbox.db')
    for role in ('lead', 'builder', 'reviewer', 'guardian'):
        store.add_agent(role)
    sender = {'id': 'lead', 'rank': 'lead'}
    ids = [store.send(sender, 'builder', str(i))['id'] for i in range(6)]
    for mid, state, stamp in zip(ids, ['delivered', 'delivered', 'queued', 'read', 'done', 'delivered'],
                                [8199, 8200, 1, 1, 1, 9999]):
        store.db.execute('UPDATE deliveries SET state=?,updated_at=? WHERE message_id=?', (state, stamp, mid))
    assert store.alert_stale_deliveries(0) == 0
    # Dispatcher checks even when all adapters are manual.
    store.alert_stale_deliveries()
    alerts = store.inbox({'id': 'guardian'}, mark=False)
    assert len(alerts) == 1
    assert f'#{ids[0]}' in alerts[0]['body'] and 'builder' in alerts[0]['body']
    store.db.close()
    store = Store(tmp_path / 'mbox.db')
    assert store.alert_stale_deliveries() == 0
    store.inbox({'id': 'guardian'})
    store.db.execute("UPDATE deliveries SET updated_at=1 WHERE recipient='guardian'")
    assert store.alert_stale_deliveries() == 0
    assert store.alert_stale_deliveries(10) == 1  # New threshold catches the boundary row.
    store.db.close()


def test_stale_delivery_missing_guardian_and_atomic_retry(tmp_path, monkeypatch):
    monkeypatch.setattr('mbox.core.time.time', lambda: 10000)
    store = Store(tmp_path / 'mbox.db')
    store.add_agent('builder')
    store.add_agent('lead')
    mid = store.send({'id': 'lead', 'rank': 'lead'}, 'builder', 'work')['id']
    store.inbox({'id': 'builder'})
    store.db.execute('UPDATE deliveries SET updated_at=1')
    assert store.alert_stale_deliveries() == 0
    store.add_agent('guardian')
    store.db.execute("""CREATE TRIGGER fail_alert BEFORE INSERT ON delivery_alerts
                        BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
    with pytest.raises(Exception, match='test failure'):
        store.alert_stale_deliveries()
    assert store.inbox({'id': 'guardian'}, mark=False) == []
    store.db.execute('DROP TRIGGER fail_alert')
    assert store.alert_stale_deliveries() == 1
    # A second dispatcher connection must still send just one alert.
    other = Store(tmp_path / 'mbox.db')
    assert other.alert_stale_deliveries() == 0
    assert len(store.inbox({'id': 'guardian'}, mark=False)) == 1
    other.db.close()
    store.db.close()


def test_zk_wrapper_defaults(tmp_path):
    (tmp_path / 'bin').mkdir()
    (tmp_path / '.venv/bin').mkdir(parents=True)
    shutil.copy2(ROOT / 'bin/aaf-chat', tmp_path / 'bin/aaf-chat')
    shutil.copy2(ROOT / 'bin/_instance_env.sh', tmp_path / 'bin/_instance_env.sh')
    python = tmp_path / '.venv/bin/python'
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$AAF_SERVER_STATE" "$@"\n')
    python.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ('AAF_CHAT_URL', 'AAF_SERVER_STATE', 'AAF_AGENT') and not k.startswith(('MBOX_', 'AAF_', 'AAF_'))}
    env['MBOX_AGENT'] = 'builder'
    result = subprocess.run([str(tmp_path / 'bin/aaf-chat'), 'rooms'], env=env, capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.splitlines() == [str(tmp_path / 'var/server'),
                                         str(tmp_path / 'server/cli.py'), '--url',
                                         'http://127.0.0.1:8111', '--agent', 'builder', 'rooms']


def test_stale_delivery_concurrent_dispatchers(tmp_path, monkeypatch):
    monkeypatch.setattr('mbox.core.time.time', lambda: 10000)
    store = Store(tmp_path / 'mbox.db')
    for role in ('lead', 'builder', 'guardian'):
        store.add_agent(role)
    store.send({'id': 'lead', 'rank': 'lead'}, 'builder', 'work')
    store.inbox({'id': 'builder'})
    store.db.execute('UPDATE deliveries SET updated_at=1')
    other = Store(tmp_path / 'mbox.db')
    barrier = threading.Barrier(2)
    def check(connection):
        barrier.wait()
        return connection.alert_stale_deliveries()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(check, (store, other)))
    assert sorted(results) == [0, 1]
    assert len(store.inbox({'id': 'guardian'}, mark=False)) == 1
    store.db.close()
    other.db.close()
