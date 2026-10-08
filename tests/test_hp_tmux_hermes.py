"""#34：tmux 房內互動 Hermes 的引擎辨識與血量。"""
import json
import sqlite3
from pathlib import Path

import pytest

import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from drivers import hermes_state as hs  # noqa: E402  v2 S4：Hermes 內部資料移到 driver

ROOT = Path(__file__).resolve().parents[1]

LAUNCHER = ("/Users/u/.hermes/tools/python-3.14.7/bin/python3 -I -c import os, re, sys\\012"
            "sys.path.insert(0, '/Users/u/.hermes/hermes-agent')\\012import hermes_bootstrap\\012"
            "from hermes_cli.main import main\\012sys.exit(main()) --yolo chat -q hi")


@pytest.fixture()
def mods(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'server'))
    monkeypatch.syspath_prepend(str(ROOT))
    import runtime, member_health
    return runtime, member_health


@pytest.mark.parametrize('comm,args,want', [
    ('/Users/u/.hermes/tools/python-3.14.7/bin/python3', LAUNCHER, 'hermes'),
    ('python3.14', LAUNCHER, 'hermes'),
    ('someverylongnam', LAUNCHER, 'hermes'),   # macOS ps comm 截 16 字：/Users/u-with-a-long-name → basename
    ('someverylongnam', 'zsh -c ' + LAUNCHER, None),
    ('python3', 'python3 -m hermes_cli.main chat', 'hermes'),
    ('hermes', 'hermes chat', 'hermes'),
    # 只有路徑子字串、沒有 launcher 兩個特徵 → 不判
    ('python3', "python3 /Users/u/.hermes/hermes-agent/tools/foo.py", None),
    ('python3', "python3 -c sys.path.insert(0, '/x/hermes-agent')", None),
    ('zsh', 'zsh -c ' + LAUNCHER, None),   # shell -c 包裝帶相同字串不判
    ('python3', 'python3 -m http.server', None),
])
def test_detect_engine_hermes_launcher(mods, comm, args, want):
    rt, _ = mods
    assert rt.detect_engine(comm, args) == want


def test_status_bar_parse(mods):
    _, mh = mods
    screen = ('earlier ☤ fake │ ~1K/2K │ ~50% │ in body text\n'
              ' ☤ claude-opus-5.5 │ ~102K/1M │ [█░░░░░░░░░] ~10% │  ─ 建置 builder 接回…\n'
              '────\n☤ ❯ msg=interrupt\n')
    bar = hs.hermes_status_bar(screen)
    assert bar == dict(model='claude-opus-5.5', percent=10, used='102K', window='1M')
    assert hs.hermes_status_bar('no bar here\n☤ ❯ \n') is None
    assert hs.hermes_status_bar(' ☤ m │ ~1K/1M │ ~150% │\n') is None


def make_home(tmp, entries, *, anchor=(91200, 0)):
    home = tmp / 'hermes'
    (home / 'runtime').mkdir(parents=True)
    (home / 'runtime' / 'active_sessions.json').write_text(json.dumps({'entries': entries}))
    (home / 'config.yaml').write_text('model:\n  default: claude-opus-5.5\n  provider: copilot\n'
                                      '  context_length: 262144\n  base_url: https://api.githubcopilot.com\n')
    (home / 'models_dev_cache.json').write_text(json.dumps(
        {'github-copilot': {'models': {'claude-opus-5.5': {'limit': {'context': 1000000}}}}}))
    db = sqlite3.connect(home / 'state.db')
    db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, model_config TEXT, last_activity_at REAL, '
               'billing_provider TEXT, billing_base_url TEXT, parent_session_id TEXT, started_at REAL, '
               'source TEXT, end_reason TEXT)')
    db.execute('INSERT INTO sessions (id, model, model_config, last_activity_at, billing_provider, '
               'billing_base_url, parent_session_id, started_at) VALUES (?,?,?,?,?,?,?,?)',
               ('S-tmux', 'claude-opus-5.5', json.dumps({'_usage_anchor': {'prompt_tokens': anchor[0],
                'completion_tokens': anchor[1]}}), 1791190400.0, 'copilot', 'https://api.enterprise.githubcopilot.com',
                None, 100.0))
    db.commit()
    db.close()
    return home


def entry(pid, start, sid):
    return dict(pid=pid, process_start_time=start, session_id=sid, metadata={'live_session_id': sid})


def test_session_for_pid(mods, tmp_path):
    _, mh = mods
    home = make_home(tmp_path, [entry(100, 1000.4, 'S-tmux'), entry(200, 5000.0, 'S-other')])
    assert hs.hermes_session_for_pid(100, home, start_time=1000.0) == ('S-tmux', None)
    # PID 重用：啟動時間對不上 → 不採用
    assert hs.hermes_session_for_pid(100, home, start_time=1900.0)[0] is None
    assert '無此程序' in hs.hermes_session_for_pid(300, home, start_time=1.0)[1]
    home2 = make_home(tmp_path / 'b', [entry(100, 1000.0, 'A'), entry(100, 1000.0, 'B')])
    sid, why = hs.hermes_session_for_pid(100, home2, start_time=1000.0)
    assert sid is None and '2 個 session' in why
    assert hs.hermes_session_for_pid(100, tmp_path / 'none', start_time=1.0)[1] == 'Hermes active_sessions.json 不可讀'


def test_session_context_uses_pin_scoping(mods, tmp_path):
    _, mh = mods
    home = make_home(tmp_path, [])
    ctx = hs.hermes_session_context('S-tmux', home)
    assert ctx['percent'] == 9 and '91200/1000000' in ctx['source'] and 'route 不同' in ctx['source']
    assert hs.hermes_session_context('nope', home)['reason'] == 'state.db 查無本角色 session'


# ── inspect_member 全路徑（假 tmux／ps） ──

class FakeRun:
    def __init__(self, screen):
        self.screen = screen

    def __call__(self, cmd, **kw):
        class R:
            returncode = 0
            stdout = ''
        r = R()
        if 'list-panes' in cmd:
            r.stdout = '%0|0|python3|10\n'
        elif 'capture-pane' in cmd:
            r.stdout = self.screen
        elif cmd[:2] == ['ps', '-o']:
            r.stdout = 'Mon Oct  5 16:52:14 2026\n'
        return r


SCREEN = (' ☤ claude-opus-5.5 │ ~102K/1M │ [█░░░░░░░░░] ~10% │  ─ builder\n'
          '────────\n☤ ❯ \n────────\n')


def run_inspect(mods, monkeypatch, tmp_path, entries, screen=SCREEN):
    rt, mh = mods
    import time
    home = make_home(tmp_path, entries)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(rt, 'agent_roles', lambda: ['builder'])
    monkeypatch.setattr(rt, 'load_roles', lambda: {'builder': {'runtime': 'hermes', 'adapter': 'hermes-headless'}})
    monkeypatch.setattr(rt, 'session_alive', lambda a: True)
    monkeypatch.setattr(rt, 'tmux_bin', lambda: 'tmux')
    monkeypatch.setattr(rt, 'process_table', lambda: {
        10: (1, 10, 10, 'zsh', 'zsh -c env …'),
        11: (10, 10, 10, 'python3', LAUNCHER)})
    monkeypatch.setattr(mh.subprocess, 'run', FakeRun(screen))
    mh._cache.clear()
    start = time.mktime(time.strptime('Mon Oct  5 16:52:14 2026', '%a %b %d %H:%M:%S %Y'))
    return mh.inspect_member('builder'), start


def test_inspect_member_tmux_hermes_state_db(mods, monkeypatch, tmp_path):
    import time
    start = time.mktime(time.strptime('Mon Oct  5 16:52:14 2026', '%a %b %d %H:%M:%S %Y'))
    r, _ = run_inspect(mods, monkeypatch, tmp_path, [entry(11, start + 0.3, 'S-tmux')])
    assert r['engine'] == 'Hermes' and r['percent'] == 9 and r['model'] == 'claude-opus-5.5'
    assert 'tmux 房 Hermes session S-tmux' in r['source'] and 'state.db' in r['source']


def test_inspect_member_tmux_hermes_bar_fallback(mods, monkeypatch, tmp_path):
    r, _ = run_inspect(mods, monkeypatch, tmp_path, [])
    assert r['engine'] == 'Hermes' and r['percent'] == 10 and r['source'].startswith('終端狀態列')
    assert 'active_sessions 無此程序的登記' in r['source']


def test_inspect_member_tmux_hermes_unknown(mods, monkeypatch, tmp_path):
    r, _ = run_inspect(mods, monkeypatch, tmp_path, [], screen='☤ ❯ \n')
    assert r['engine'] == 'Hermes' and r['percent'] is None
    assert '畫面也無 Hermes 狀態列' in r['percent_reason']


def add_child(home, sid, parent, started, prompt, *, source='cli', extra=None, compress_parent=True):
    cfg = {'_usage_anchor': {'prompt_tokens': prompt, 'completion_tokens': 0}, **(extra or {})}
    db = sqlite3.connect(home / 'state.db')
    if compress_parent:
        db.execute("UPDATE sessions SET end_reason='compression' WHERE id=?", (parent,))
    db.execute('INSERT INTO sessions (id, model, model_config, last_activity_at, billing_provider, billing_base_url, '
               'parent_session_id, started_at, source) VALUES (?,?,?,?,?,?,?,?,?)',
               (sid, 'claude-opus-5.5', json.dumps(cfg), started, 'copilot',
                'https://api.enterprise.githubcopilot.com', parent, started, source))
    db.commit()
    db.close()


def test_session_tip_follows_compression_chain(mods, tmp_path):
    """reviewer #997 SHOULD：parent→child→grandchild 必須取到 grandchild；同層取 started_at 最大者。"""
    _, mh = mods
    home = make_home(tmp_path, [])
    add_child(home, 'C-old', 'S-tmux', 200.0, 1)
    add_child(home, 'C', 'S-tmux', 300.0, 2)
    add_child(home, 'G', 'C', 400.0, 50000)
    assert hs.hermes_session_tip('S-tmux', home) == ('G', 2)
    assert hs.hermes_session_tip('G', home) == ('G', 0)
    assert hs.hermes_session_tip('X', tmp_path / 'nohome') == ('X', 0)


def test_inspect_member_uses_chain_tip(mods, monkeypatch, tmp_path):
    import time
    rt, mh = mods
    start = time.mktime(time.strptime('Mon Oct  5 16:52:14 2026', '%a %b %d %H:%M:%S %Y'))
    home = make_home(tmp_path / 'pre', [])  # 只為建立目錄結構；實際 home 由 run_inspect 建
    orig = hs.hermes_session_tip

    def tip(sid, home=None):
        add_child(hs.hermes_home(), 'C', 'S-tmux', 300.0, 1000)
        add_child(hs.hermes_home(), 'G', 'C', 400.0, 50000)
        monkeypatch.setattr(hs, 'hermes_session_tip', orig)
        return orig(sid, home)
    monkeypatch.setattr(hs, 'hermes_session_tip', tip)
    r, _ = run_inspect(mods, monkeypatch, tmp_path, [entry(11, start, 'S-tmux')])
    assert r['percent'] == 5 and 'session G（經壓縮鏈 2 層）' in r['source'] and '50000/1000000' in r['source']


def test_tip_ignores_subagent_children_without_compression(mods, tmp_path):
    """reviewer #1010 (a)：父未壓縮，底下只有 subagent／delegate 子 → tip = 原 sid。"""
    _, mh = mods
    home = make_home(tmp_path, [])
    for i in range(4):
        add_child(home, f'SUB{i}', 'S-tmux', 200.0 + i, 9, source='subagent',
                  extra={'_delegate_from': 'S-tmux'}, compress_parent=False)
    assert hs.hermes_session_tip('S-tmux', home) == ('S-tmux', 0)


def test_tip_ignores_branched_child(mods, tmp_path):
    """reviewer #1010 (b)：父已壓縮，但唯一 child 帶 _branched_from → 不往下走。"""
    _, mh = mods
    home = make_home(tmp_path, [])
    add_child(home, 'BR', 'S-tmux', 300.0, 9, extra={'_branched_from': 'S-tmux'})
    assert hs.hermes_session_tip('S-tmux', home) == ('S-tmux', 0)


def test_tip_prefers_compression_child_over_delegate(mods, tmp_path):
    """reviewer #1010 (c)：父已壓縮，同時有 delegate child（較新）與壓縮 child → 取壓縮 child；tool child 也不走。"""
    _, mh = mods
    home = make_home(tmp_path, [])
    add_child(home, 'COMP', 'S-tmux', 300.0, 9)
    add_child(home, 'DEL', 'S-tmux', 500.0, 9, source='subagent', extra={'_delegate_from': 'S-tmux'})
    add_child(home, 'TOOL', 'S-tmux', 600.0, 9, source='tool')
    assert hs.hermes_session_tip('S-tmux', home) == ('COMP', 1)


def test_tip_cycle_and_depth_cap(mods, tmp_path, monkeypatch):
    _, mh = mods
    home = make_home(tmp_path, [])
    add_child(home, 'A', 'S-tmux', 300.0, 9)
    add_child(home, 'B', 'A', 400.0, 9)
    monkeypatch.setattr(hs, 'TIP_MAX_DEPTH', 1)
    assert hs.hermes_session_tip('S-tmux', home) == ('A', 1)


def test_tip_ignores_reset_fork_child(mods, tmp_path):
    """reviewer #1115：父已壓縮，唯一 child 帶 _reset_from（reset fork，另一段對話）→ 不往下追。"""
    _, mh = mods
    home = make_home(tmp_path, [])
    add_child(home, 'RST', 'S-tmux', 300.0, 9, extra={'_reset_from': 'S-tmux'})
    assert hs.hermes_session_tip('S-tmux', home) == ('S-tmux', 0)
