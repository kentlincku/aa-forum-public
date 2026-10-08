"""任務 #18：AA Forum Enter 送出（IME 安全）＋ Hermes headless context 血量。"""
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from drivers import hermes_state as hs  # noqa: E402  v2 S4：Hermes 內部資料移到 driver

ROOT = Path(__file__).resolve().parent.parent


# ── Hermes headless context 已用 %（假 state.db） ──

@pytest.fixture()
def mh(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'server'))
    monkeypatch.syspath_prepend(str(ROOT))
    import member_health
    return member_health


def make_home(tmp, *, model='claude-opus-5.5', window=262144, anchor=(50314, 590), sid='S1',
              row_model=None, config_text=None):
    home = tmp / 'hermes'
    home.mkdir()
    (home / 'config.yaml').write_text(config_text if config_text is not None else
                                      f'model:\n  default: {model}\n  provider: copilot\n'
                                      f'  context_length: {window}\n  max_tokens: 32768\nagent:\n  x: 1\n')
    db = sqlite3.connect(home / 'state.db')
    # 只建本功能讀取的欄位（實際 schema 為 ~/.hermes/state.db sessions 的子集）
    db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, model_config TEXT, last_activity_at REAL, '
               'billing_provider TEXT, billing_base_url TEXT)')
    cfg = {'max_iterations': 150}
    if anchor is not None:
        cfg['_usage_anchor'] = {'prompt_tokens': anchor[0], 'completion_tokens': anchor[1], 'base_count': 30}
    db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?)', ('S1', row_model or model, json.dumps(cfg), 1791184483.06,
                                                              'copilot', 'https://api.enterprise.githubcopilot.com'))
    db.commit()
    db.close()
    state = tmp / 'role'
    state.mkdir()
    if sid is not None:
        (state / 'session.hermes').write_text(sid + '\n')
    return home, state


def test_hermes_context_percent(mh, tmp_path):
    home, state = make_home(tmp_path)
    ctx = hs.hermes_context(state, home)
    assert ctx['percent'] == round(100 * (50314 + 590) / 262144) == 19
    assert ctx['model'] == 'claude-opus-5.5' and ctx['measured_at'] == 1791184483.06
    assert '50904/262144' in ctx['source'] and 'config.yaml' in ctx['source']


@pytest.mark.parametrize('kw,why', [
    (dict(sid=None), '尚無 Hermes session'),
    (dict(sid='NOPE'), '查無'),
    (dict(anchor=None), '尚無用量'),
    (dict(row_model='other-model'), '查不到模型 other-model'),   # 非預設模型且各來源都查不到
    (dict(config_text='agent:\n  x: 1\n'), '查不到模型'),
    (dict(anchor=(300000, 0)), '超出'),
])
def test_hermes_context_unknown(mh, tmp_path, kw, why):
    home, state = make_home(tmp_path, **kw)
    ctx = hs.hermes_context(state, home)
    assert 'percent' not in ctx and why in ctx['reason']


def test_non_default_model_window_sources(mh, tmp_path):
    """#19 追加：非 config 預設模型依序查 roles context_length → Hermes 快取 → models.dev。"""
    home, state = make_home(tmp_path, row_model='gpt-5.4', anchor=(105000, 0))
    assert 'percent' not in hs.hermes_context(state, home)
    (home / 'models_dev_cache.json').write_text(json.dumps(
        {'github-copilot': {'models': {'gpt-5.4': {'limit': {'context': 1050000}}}}}))
    ctx = hs.hermes_context(state, home)
    assert ctx['percent'] == 10 and 'models.dev' in ctx['source']
    (home / 'context_length_cache.yaml').write_text(
        'context_lengths:\n  openai/gpt-5.4@https://api.enterprise.githubcopilot.com: 210000\n')
    ctx = hs.hermes_context(state, home)
    assert ctx['percent'] == 50 and '快取' in ctx['source']
    ctx = hs.hermes_context(state, home, cfg_window=420000, cfg_model='gpt-5.4')
    assert ctx['percent'] == 25 and 'roles' in ctx['source']
    # roles.json 上限屬於別的模型 → 不採用，回到快取
    ctx = hs.hermes_context(state, home, cfg_window=420000, cfg_model='claude-opus-5.5')
    assert ctx['percent'] == 50 and '快取' in ctx['source']
    assert hs.hermes_context(state, home, cfg_window=True, cfg_model='gpt-5.4')['percent'] == 50  # bool 不當上限


def test_headless_status_reports_reason(mh, tmp_path, monkeypatch):
    import runtime as rt
    home, state = make_home(tmp_path, row_model='mystery-1')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(rt, 'MBOX_HOME', tmp_path / 'mbox')
    (tmp_path / 'mbox' / 'roles').mkdir(parents=True)
    (tmp_path / 'mbox' / 'roles' / 'builder').symlink_to(state)
    monkeypatch.setattr(rt, 'load_roles', lambda: {'builder': {'runtime': 'hermes', 'adapter': 'hermes-headless',
                                                              'workdir': str(tmp_path / 'wd')}})
    monkeypatch.setattr(rt, 'unread_count', lambda a: 0)
    r = mh.headless_status('builder', dict(percent=None))
    assert r['percent'] is None and '查不到模型 mystery-1' in r['percent_reason']
    assert '血量未知' in r['source']


def test_hermes_context_no_db(mh, tmp_path):
    home, state = make_home(tmp_path)
    (home / 'state.db').unlink()
    assert 'percent' not in hs.hermes_context(state, home)
    assert not (home / 'state.db').exists()  # 唯讀連線不會建出新檔


def test_hermes_context_readonly(mh, tmp_path):
    home, state = make_home(tmp_path)
    before = (home / 'state.db').read_bytes()
    hs.hermes_context(state, home)
    assert (home / 'state.db').read_bytes() == before


# (Enter/IME behaviour now lives in web/src/lib/enter.ts with its own tests.)

def run_status(mh, tmp_path, monkeypatch, *, session_model, override=None, catalog=None):
    import runtime as rt
    home, state = make_home(tmp_path, row_model=session_model)
    if catalog:
        (home / 'models_dev_cache.json').write_text(json.dumps({'github-copilot': {'models': catalog}}))
    monkeypatch.setenv('HERMES_HOME', str(home))
    mbox = tmp_path / 'mbox'
    (mbox / 'roles').mkdir(parents=True)
    (mbox / 'roles' / 'builder').symlink_to(state)
    if override:
        (state / 'override.json').write_text(json.dumps({'model': override}))
    monkeypatch.setattr(rt, 'MBOX_HOME', mbox)
    monkeypatch.setattr(rt, 'load_roles', lambda: {'builder': {'runtime': 'hermes', 'adapter': 'hermes-headless',
                                                              'workdir': str(tmp_path / 'wd')}})
    monkeypatch.setattr(rt, 'unread_count', lambda a: 0)
    return mh.headless_status('builder', dict(percent=None))


def test_hp_override_model_found(mh, tmp_path, monkeypatch):
    r = run_status(mh, tmp_path, monkeypatch, session_model='gpt-5.4', override='gpt-5.4',
                   catalog={'gpt-5.4': {'limit': {'context': 1050000}}})
    assert r['percent'] == round(100 * 50904 / 1050000) and r['model'] == 'gpt-5.4' and 'models.dev' in r['source']


def test_hp_override_model_not_found(mh, tmp_path, monkeypatch):
    r = run_status(mh, tmp_path, monkeypatch, session_model='mystery-2', override='mystery-2')
    assert r['percent'] is None and '查不到模型 mystery-2' in r['percent_reason']


def test_hp_default_model(mh, tmp_path, monkeypatch):
    r = run_status(mh, tmp_path, monkeypatch, session_model='claude-opus-5.5')
    assert r['percent'] == 19 and 'config.yaml' in r['source']


def test_hp_roles_window_not_reused_for_override_model(mh, tmp_path, monkeypatch):
    """反例（reviewer 複審 SHOULD／lead #549）：roles.json 有 model+context_length，override 換成別的模型。"""
    import runtime as rt
    r = run_status(mh, tmp_path, monkeypatch, session_model='mystery-3', override='mystery-3')
    roles = {'builder': {'runtime': 'hermes', 'adapter': 'hermes-headless', 'workdir': str(tmp_path / 'wd'),
                       'model': 'claude-opus-5.5', 'context_length': 100000}}
    monkeypatch.setattr(rt, 'load_roles', lambda: roles)
    r = mh.headless_status('builder', dict(percent=None))
    assert r['percent'] is None and '查不到模型 mystery-3' in r['percent_reason']


def test_hp_roles_window_used_for_same_model(mh, tmp_path, monkeypatch):
    """#25 正例：roles.json model A + context_length X，session 也是 A → 採用 X（全路徑）。"""
    import runtime as rt
    run_status(mh, tmp_path, monkeypatch, session_model='model-a')
    monkeypatch.setattr(rt, 'load_roles', lambda: {'builder': {
        'runtime': 'hermes', 'adapter': 'hermes-headless', 'workdir': str(tmp_path / 'wd'),
        'model': 'model-a', 'context_length': 101808}})
    r = mh.headless_status('builder', dict(percent=None))
    assert r['percent'] == 50 and 'roles' in r['source']   # 50904/101808


def test_hp_roles_window_not_used_when_override_differs(mh, tmp_path, monkeypatch):
    """#25 反例：roles.json model A + X，override B（帶 context_length 也不得採用），session B → 不用 X。"""
    import runtime as rt
    run_status(mh, tmp_path, monkeypatch, session_model='model-b',
               catalog={'model-b': {'limit': {'context': 509040}}})
    (tmp_path / 'mbox' / 'roles' / 'builder' / 'override.json').write_text(
        json.dumps({'model': 'model-b', 'context_length': 101808}))
    monkeypatch.setattr(rt, 'load_roles', lambda: {'builder': {
        'runtime': 'hermes', 'adapter': 'hermes-headless', 'workdir': str(tmp_path / 'wd'),
        'model': 'model-a', 'context_length': 101808}})
    r = mh.headless_status('builder', dict(percent=None))
    assert r['percent'] == 10 and 'models.dev' in r['source']   # 50904/509040，不是 50%


# ── #33：config pin 只在 session route = config 預設 route 時採用（對齊 Hermes pin scoping） ──

PIN_CATALOG = {'github-copilot': {'models': {'claude-opus-5.5': {'limit': {'context': 1000000}}}}}


def pin_home(tmp_path, *, base_url, provider='copilot'):
    cfg = (f'model:\n  default: claude-opus-5.5\n  provider: {provider}\n  context_length: 262144\n'
           f'  base_url: {base_url}\nagent:\n  x: 1\n')
    home, state = make_home(tmp_path, config_text=cfg, anchor=(420000, 0))
    (home / 'models_dev_cache.json').write_text(json.dumps(PIN_CATALOG))
    return home, state


def test_pin_used_when_route_matches(mh, tmp_path):
    # 尾端斜線、大寫 host 依 normalize_route_base_url 視為同 route
    home, state = pin_home(tmp_path, base_url='https://API.enterprise.githubcopilot.com/')
    ctx = hs.hermes_context(state, home)
    assert 'percent' not in ctx and '262144' in ctx['reason'] and 'config.yaml' in ctx['reason']  # 420000 > pin


def test_pin_not_used_when_base_url_differs(mh, tmp_path):
    home, state = pin_home(tmp_path, base_url='https://api.githubcopilot.com')
    ctx = hs.hermes_context(state, home)
    assert ctx['percent'] == 42 and '420000/1000000' in ctx['source'] and 'models.dev' in ctx['source']
    assert 'config pin 不適用：route 不同' in ctx['source'] and 'enterprise' in ctx['source']


def test_pin_not_used_when_provider_differs(mh, tmp_path):
    """config 沒寫 base_url 時才比 provider（Hermes _context_route_mismatch 的 fallback）。"""
    cfg = 'model:\n  default: claude-opus-5.5\n  provider: openrouter\n  context_length: 262144\nagent:\n  x: 1\n'
    home, state = make_home(tmp_path, config_text=cfg, anchor=(420000, 0))
    (home / 'models_dev_cache.json').write_text(json.dumps({'openrouter': {'models': {}}, **PIN_CATALOG}))
    ctx = hs.hermes_context(state, home)
    assert ctx['percent'] == 42 and 'config pin 不適用：provider 不同' in ctx['source']


def test_pin_used_when_provider_alias_but_route_same(mh, tmp_path):
    """reviewer #895 NOTE 1：config 有 base_url 時只比 route；provider 名稱不同（別名）但 route 相同 → 採用 pin。"""
    home, state = pin_home(tmp_path, base_url='https://api.enterprise.githubcopilot.com', provider='github-copilot')
    ctx = hs.hermes_context(state, home)
    assert 'percent' not in ctx and '262144' in ctx['reason'] and 'config.yaml' in ctx['reason']


def test_context_cache_url_normalized(mh, tmp_path):
    """reviewer #895 NOTE 3：快取 key 的 url 與 session base_url 尾斜線、大小寫不同也能命中。"""
    home, state = make_home(tmp_path, row_model='gpt-5.4', anchor=(105000, 0))
    (home / 'context_length_cache.yaml').write_text(
        'context_lengths:\n  openai/gpt-5.4@https://API.Enterprise.githubcopilot.com/: 210000\n')
    ctx = hs.hermes_context(state, home)
    assert ctx['percent'] == 50 and '快取' in ctx['source']


def test_normalize_route_base_url_matches_hermes(mh):
    n = hs.normalize_route_base_url
    assert n('https://API.x.com/') == n('https://api.x.com') == 'https://api.x.com'
    assert n('https://api.x.com:443/v1/') == 'https://api.x.com/v1'
    assert n('') == '' and n(None) == ''
