"""#40-3：dispatcher 代報角色心跳（wake→busy、輪次結束→idle）與名冊 runtime 依 roles.json 同步。"""
from types import SimpleNamespace

import pytest

from mbox import dispatcher
from mbox.core import Store


class FakeAdapter:
    """headless 假 adapter：wake 後 running（busy），finish_turn() 後 running.pid 消失（idle）。"""
    level = 'headless'

    def __init__(self):
        self.running = False
        self.woken = 0

    def health(self):
        return 'busy' if self.running else 'idle'

    def wake(self, n, head=None, preview=''):
        self.running = True
        self.woken += 1
        return 'started fake'

    def completion_status(self):
        return 0, ''


@pytest.fixture
def env(tmp_path, monkeypatch):
    store = Store(tmp_path / 'hb.db')
    store.add_agent('lead', 'claude', 'lead')
    store.add_agent('builder', 'codex')
    ad = FakeAdapter()
    monkeypatch.setattr(dispatcher.adapters, 'make', lambda role, cfg, home: ad)
    monkeypatch.setattr(dispatcher.adapters, 'tmux_room_exists', lambda _: False)
    monkeypatch.setattr(dispatcher.adapters, 'render_preview', lambda msgs, ids: ids.extend(m['id'] for m in msgs) or 'p')
    monkeypatch.setattr(dispatcher, 'home', lambda: tmp_path)
    yield store, ad
    store.db.close()


def agent(store, role):
    return next(a for a in store.agents() if a['id'] == role)


def test_heartbeat_transitions(env):
    store, ad = env
    roles = {'builder': {'adapter': 'fake', 'runtime': 'hermes'}}
    assert agent(store, 'builder')['last_heartbeat'] is None
    # 無未讀：閒置輪 → idle
    dispatcher.once(roles, store, log=lambda s: None)
    a = agent(store, 'builder')
    assert a['status'] == 'idle' and a['last_heartbeat']
    # 有未讀 → wake → busy
    store.send({'id': 'lead', 'rank': 'lead'}, 'builder', 'work')
    dispatcher.once(roles, store, log=lambda s: None)
    assert ad.woken == 1 and agent(store, 'builder')['status'] == 'busy'
    # 輪次進行中仍 busy，心跳時間前進
    t1 = agent(store, 'builder')['last_heartbeat']
    dispatcher.once(roles, store, log=lambda s: None)
    a = agent(store, 'builder')
    assert a['status'] == 'busy' and a['last_heartbeat'] >= t1
    # 輪次結束（running.pid 消失）→ idle
    ad.running = False
    store.ack({'id': 'builder'}, store.inbox({'id': 'builder'}, mark=False)[0]['id'], 'done')
    dispatcher.once(roles, store, log=lambda s: None)
    assert agent(store, 'builder')['status'] == 'idle'


def test_manual_and_unknown_not_proxied(env, monkeypatch):
    store, ad = env
    dispatcher.once({'builder': {'adapter': 'manual'}}, store, log=lambda s: None)
    assert agent(store, 'builder')['last_heartbeat'] is None
    monkeypatch.setattr(ad, 'health', lambda: 'unknown')
    dispatcher.once({'builder': {'adapter': 'fake'}}, store, log=lambda s: None)
    assert agent(store, 'builder')['last_heartbeat'] is None


def test_proxy_heartbeat_rejects_other_status(env):
    store, _ = env
    from mbox.core import MboxError
    with pytest.raises(MboxError):
        store.proxy_heartbeat('builder', 'alive')


def test_runtime_sync_on_setup(env, tmp_path, capsys):
    store, _ = env
    roles = {'lead': {'runtime': 'hermes', 'rank': 'lead'}, 'builder': {'runtime': 'hermes'},
             'ghost': {'runtime': 'hermes'}}
    (tmp_path / 'tokens').mkdir()
    for r in ('lead', 'builder'):
        (tmp_path / 'tokens' / r).write_text('x')
    dispatcher.setup(roles, store)
    rt = {a['id']: a['runtime'] for a in store.agents()}
    assert rt['lead'] == 'hermes' and rt['builder'] == 'hermes'
    assert rt['server'] == 'system'  # 不在 roles.json 的 system 身分不動
    out = capsys.readouterr().out
    assert 'lead       runtime claude → hermes' in out and 'builder    runtime codex → hermes' in out
    assert store.sync_runtimes(roles) == []  # 冪等


def test_runtime_sync_on_loop_start(env, monkeypatch):
    store, _ = env
    logs = []
    monkeypatch.setattr(dispatcher, 'once', lambda *a, **kw: True)
    monkeypatch.setattr(dispatcher, 'heartbeat', lambda log=print: None)

    def stop(_):
        raise KeyboardInterrupt
    monkeypatch.setattr(dispatcher.time, 'sleep', stop)
    with pytest.raises(KeyboardInterrupt):
        dispatcher.loop({'builder': {'runtime': 'pi'}}, store, log=logs.append)
    assert agent(store, 'builder')['runtime'] == 'pi'
    assert any('builder: 名冊 runtime codex → pi' in s for s in logs)


def test_self_report_not_overwritten_within_5_min(env, monkeypatch):
    """reviewer #42 SHOULD 1：角色 5 分鐘內自報過 → 代報不覆蓋；超過才代報並標 proxy。"""
    store, ad = env
    roles = {'builder': {'adapter': 'fake'}}
    store.heartbeat({'id': 'builder'}, 'busy', 42.0)
    a = agent(store, 'builder')
    assert a['heartbeat_source'] == 'self' and a['status'] == 'busy'
    dispatcher.once(roles, store, log=lambda s: None)  # adapter idle，但角色剛自報 busy
    a2 = agent(store, 'builder')
    assert a2['status'] == 'busy' and a2['heartbeat_source'] == 'self' and a2['last_heartbeat'] == a['last_heartbeat']
    store.db.execute('UPDATE agents SET self_heartbeat=self_heartbeat-301 WHERE id=?', ('builder',))
    dispatcher.once(roles, store, log=lambda s: None)
    a3 = agent(store, 'builder')
    assert a3['status'] == 'idle' and a3['heartbeat_source'] == 'proxy' and a3['context_pct'] == 42.0


def test_tmux_adapter_only_proxies_busy(env, monkeypatch):
    store, ad = env
    ad.level = 'tmux'
    dispatcher.once({'builder': {'adapter': 'fake'}}, store, log=lambda s: None)
    assert agent(store, 'builder')['last_heartbeat'] is None  # tmux 的 idle 不代報
    ad.running = True
    dispatcher.once({'builder': {'adapter': 'fake'}}, store, log=lambda s: None)
    a = agent(store, 'builder')
    assert a['status'] == 'busy' and a['heartbeat_source'] == 'proxy'


def test_cli_agents_marks_proxy(env, monkeypatch, capsys):
    store, _ = env
    store.proxy_heartbeat('builder', 'idle')
    from mbox import cli
    monkeypatch.setattr(cli, 'call', lambda a, m, p, b=None: store.agents())
    cli.main(['agents'])
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if l.startswith('builder'))
    assert line.endswith('(代)')
    assert not next(l for l in out.splitlines() if l.startswith('lead')).endswith('(代)')


def test_sync_reports_missing_runtime(env):
    store, _ = env
    assert store.sync_runtimes({'builder': {'adapter': 'x'}}) == [('builder', 'codex', None)]
    assert agent(store, 'builder')['runtime'] == 'codex'
