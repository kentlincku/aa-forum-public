"""#46：血量改用實際生效的 model（override → roles.json → 最近一輪 init → state.db）。
v2 S4：「最近一輪 init」改由核心 TurnResult（model_used＋session_id）提供，不再讀 prev_out.txt。"""
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CATALOG = {'github-copilot': {'models': {'claude-opus-5.5': {'limit': {'context': 1000000}},
                                         'gpt-6-astra': {'limit': {'context': 1050000}},
                                         'gpt-6-sol': {'limit': {'context': 400000}}}}}


@pytest.fixture()
def mods(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'server'))
    monkeypatch.syspath_prepend(str(ROOT))
    import runtime, member_health
    return runtime, member_health


def setup(tmp_path, monkeypatch, mods, *, roles_cfg=None, override=None, init_model=None, used=210000):
    rt, mh = mods
    home = tmp_path / 'hermes'
    home.mkdir(parents=True)
    (home / 'config.yaml').write_text('model:\n  default: claude-opus-5.5\n  provider: copilot\n'
                                      '  context_length: 262144\n  base_url: https://api.githubcopilot.com\n')
    (home / 'models_dev_cache.json').write_text(json.dumps(CATALOG))
    db = sqlite3.connect(home / 'state.db')
    db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, model_config TEXT, last_activity_at REAL, '
               'billing_provider TEXT, billing_base_url TEXT)')
    db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?)',
               ('S1', 'claude-opus-5.5', json.dumps({'_usage_anchor': {'prompt_tokens': used, 'completion_tokens': 0}}),
                1791190000.0, 'copilot', 'https://api.enterprise.githubcopilot.com'))
    db.commit(); db.close()
    mbox = tmp_path / 'mbox'
    state = mbox / 'roles' / 'guardian'
    state.mkdir(parents=True)
    (state / 'session.hermes').write_text('S1\n')
    if override:
        (state / 'override.json').write_text(json.dumps(override))
    turn = {'session_id': 'S1', 'model_used': init_model, 'ok': True} if init_model else None
    monkeypatch.setattr(rt, 'last_turn', lambda a, ok_only=False: turn)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(rt, 'MBOX_HOME', mbox)
    cfg = {'runtime': 'hermes', 'adapter': 'hermes-headless', **(roles_cfg or {})}
    monkeypatch.setattr(rt, 'load_roles', lambda: {'guardian': cfg})
    monkeypatch.setattr(rt, 'unread_count', lambda a: 0)
    return mh.headless_status('guardian', dict(percent=None))


def test_override_differs_from_session_model(mods, tmp_path, monkeypatch):
    """反例（reviewer #1306）：override＝init＝gpt-6-astra（已跑過一輪），state.db 仍記 claude-opus-5.5 → 顯示 astra、分母 1.05M。"""
    r = setup(tmp_path, monkeypatch, mods, override={'model': 'gpt-6-astra', 'provider': 'copilot'},
              init_model='gpt-6-astra')
    assert r['model'] == 'gpt-6-astra' and r['percent'] == 20  # 210000/1050000
    assert '210000/1050000' in r['source'] and '模型取自 最近一輪 init' in r['source']
    assert 'state.db 未更新（仍記 claude-opus-5.5）' in r['source'] and '下一輪生效' not in r['source']


def test_override_pending_uses_init(mods, tmp_path, monkeypatch):
    """reviewer #48 SHOULD：override＝B（gpt-6-sol）、init＝A（gpt-6-astra）→ 顯示 A、用 A 的分母，註明 B 下一輪生效。"""
    r = setup(tmp_path, monkeypatch, mods, override={'model': 'gpt-6-sol'}, init_model='gpt-6-astra')
    assert r['model'] == 'gpt-6-astra' and '210000/1050000' in r['source']
    assert 'override gpt-6-sol 下一輪生效，目前仍為 gpt-6-astra' in r['source']


def test_override_without_init(mods, tmp_path, monkeypatch):
    """還沒有本 session 的 init（例如剛設 override、prev_out 不存在）→ 才用 override。"""
    r = setup(tmp_path, monkeypatch, mods, override={'model': 'gpt-6-astra'})
    assert r['model'] == 'gpt-6-astra' and 'AA Forum override（尚無本 session 的 init）' in r['source']


def test_no_override_unchanged(mods, tmp_path, monkeypatch):
    """沒有 override、roles.json 沒寫 model、無 init → 行為與舊版相同（state.db 的 model）。"""
    r = setup(tmp_path, monkeypatch, mods)
    assert r['model'] == 'claude-opus-5.5' and r['percent'] == 21  # 210000/1M（route 不同，pin 不適用）
    assert '模型取自 state.db sessions.model' in r['source'] and '未更新' not in r['source']


def test_roles_json_model(mods, tmp_path, monkeypatch):
    r = setup(tmp_path, monkeypatch, mods, roles_cfg={'model': 'gpt-6-sol', 'provider': 'copilot'})
    assert r['model'] == 'gpt-6-sol' and '210000/400000' in r['source'] and '模型取自 roles.json' in r['source']


def test_override_beats_roles_json(mods, tmp_path, monkeypatch):
    r = setup(tmp_path, monkeypatch, mods, roles_cfg={'model': 'gpt-6-sol'}, override={'model': 'gpt-6-astra'})
    assert r['model'] == 'gpt-6-astra'


def test_init_beats_roles_json(mods, tmp_path, monkeypatch):
    r = setup(tmp_path, monkeypatch, mods, roles_cfg={'model': 'gpt-6-sol'}, init_model='gpt-6-astra')
    assert r['model'] == 'gpt-6-astra' and '模型取自 最近一輪 init' in r['source']


def test_init_model_used_when_no_override(mods, tmp_path, monkeypatch):
    r = setup(tmp_path, monkeypatch, mods, init_model='gpt-6-astra')
    assert r['model'] == 'gpt-6-astra' and '模型取自 最近一輪 init' in r['source']


def test_init_of_other_session_ignored(mods, tmp_path, monkeypatch):
    rt, mh = mods
    r = setup(tmp_path, monkeypatch, mods)
    monkeypatch.setattr(rt, 'last_turn', lambda a, ok_only=False: {'session_id': 'OTHER', 'model_used': 'gpt-6-astra', 'ok': True})
    mh._cache.clear()
    r = mh.headless_status('guardian', dict(percent=None))
    assert r['model'] == 'claude-opus-5.5'


def test_roles_context_length_bound_to_roles_model(mods, tmp_path, monkeypatch):
    """roles.json 的 context_length 只綁 roles.json 的 model；override 換別的模型時不沿用。"""
    r = setup(tmp_path, monkeypatch, mods, roles_cfg={'model': 'gpt-6-sol', 'context_length': 300000},
              override={'model': 'gpt-6-astra'})
    assert '210000/1050000' in r['source']
    r = setup(tmp_path / 'b', monkeypatch, mods, roles_cfg={'model': 'gpt-6-sol', 'context_length': 300000})
    assert '210000/300000' in r['source']


def test_unknown_effective_model_reason(mods, tmp_path, monkeypatch):
    r = setup(tmp_path, monkeypatch, mods, override={'model': 'mystery-9'})
    assert r['percent'] is None and '查不到模型 mystery-9' in r['percent_reason'] and 'AA Forum override' in r['percent_reason']


def test_tmux_status_bar_model_wins(mods):
    rt, mh = mods
    # 由 inspect_member tmux 分支組出的 effective（bar model）→ hermes_session_context 會用它重查分母
    from drivers import hermes_state as hs
    assert hs.hermes_status_bar(' ☤ gpt-6-astra │ ~10K/1.05M │ [░] ~1% │\n')['model'] == 'gpt-6-astra'


def test_override_round_failed_keeps_old_init(mods, tmp_path, monkeypatch):
    """lead #1348 反例：override 那一輪失敗（Hermes 不接受新模型，沒產生新的 init），
    prev_out 仍是舊模型那輪 → 顯示舊模型、舊模型的分母，並註明 override 下一輪生效。"""
    rt, mh = mods
    setup(tmp_path, monkeypatch, mods, override={'model': 'gpt-6-sol'}, init_model='claude-opus-5.5')
    # v2：失敗輪也記 TurnResult（model_used＝覆寫的新模型、ok=False）；用量仍對應最近一輪「成功」的舊模型
    turns = {False: {'session_id': 'S1', 'model_used': 'gpt-6-sol', 'ok': False, 'error': 'model not supported: gpt-6-sol'},
             True: {'session_id': 'S1', 'model_used': 'claude-opus-5.5', 'ok': True}}
    monkeypatch.setattr(rt, 'last_turn', lambda a, ok_only=False: turns[ok_only])
    mh._cache.clear()
    r = mh.headless_status('guardian', dict(percent=None))
    assert r['status'] == 'api_error' and 'model not supported' in r['source']
    assert '210000/1000000' in r['source']
    assert 'override gpt-6-sol 下一輪生效，目前仍為 claude-opus-5.5' in r['source']
