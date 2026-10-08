"""任務 #19：AA Forum 替 headless 角色切換模型（override.json，下一輪生效）。"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def ad(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    import adapters
    return adapters


CFG = {'runtime': 'hermes', 'adapter': 'hermes-headless', 'model': None, 'workdir': None}


def cfg(tmp_path, **kw):
    return dict(CFG, workdir=str(tmp_path / 'wd'), **kw)


def write_over(home, role, data):
    p = home / 'roles' / role / 'override.json'
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data if isinstance(data, str) else json.dumps(data))


def test_override_merge_and_next_argv(ad, tmp_path):
    home = tmp_path / 'mbox'
    a = ad.make('builder', cfg(tmp_path), home)
    assert '-m' not in a.argv('hi')
    write_over(home, 'builder', {'model': 'gpt-5.6', 'provider': 'copilot', 'runtime': 'codex'})
    a = ad.make('builder', cfg(tmp_path), home)          # dispatcher 每輪都重新 make
    argv = a.argv('hi')
    assert argv[argv.index('-m') + 1] == 'gpt-5.6'
    assert argv[argv.index('--provider') + 1] == 'copilot'
    assert a.cfg['runtime'] == 'hermes' and type(a).__name__ == 'HermesHeadless'  # 不可切 runtime


def test_override_beats_roles_json(ad, tmp_path):
    home = tmp_path / 'mbox'
    write_over(home, 'reviewer', {'model': 'claude-opus-5.5'})
    a = ad.make('reviewer', cfg(tmp_path, model='old-model'), home)
    assert a.argv('x')[a.argv('x').index('-m') + 1] == 'claude-opus-5.5'


def test_resume_kept_with_new_model(ad, tmp_path):
    home = tmp_path / 'mbox'
    write_over(home, 'builder', {'model': 'm2'})
    a = ad.make('builder', cfg(tmp_path), home)
    a.save_session('S1')
    argv = a.argv('x')
    assert argv[-2:] == ['--resume', 'S1'] and 'm2' in argv


@pytest.mark.parametrize('bad', ['--yolo', 'a b', 'x;rm', '', '-m', 'é'])
def test_invalid_override_ignored(ad, tmp_path, bad):
    home = tmp_path / 'mbox'
    write_over(home, 'builder', {'model': bad, 'provider': bad})
    assert '-m' not in ad.make('builder', cfg(tmp_path), home).argv('x')


@pytest.mark.parametrize('raw', ['{not json', '[1,2]', '"str"'])
def test_corrupt_override_ignored(ad, tmp_path, raw):
    home = tmp_path / 'mbox'
    write_over(home, 'builder', raw)
    assert '-m' not in ad.make('builder', cfg(tmp_path), home).argv('x')


# ── AA Forum Owner API 寫入端 ──

class _Writer:
    """Thin adapter so the tests read like the old API: set_model_override(role, model, provider)."""
    def __init__(self, home, roles):
        from mbox import overrides
        self.o, self.home, self.roles = overrides, home, roles
        self.ControlError = overrides.OverrideError
    def set_model_override(self, role, model, provider=''):
        return self.o.set_override(role, self.roles[role], self.home, model, provider)
    def model_override_info(self, role):
        return self.o.override_info(role, self.roles[role], self.home)


@pytest.fixture()
def ac(tmp_path, ad):
    roles = {'builder': cfg(tmp_path), 'owner': {'runtime': 'human', 'rank': 'human', 'driver': 'manual'}}
    return _Writer(tmp_path / 'mbox', roles), tmp_path / 'mbox'


def test_set_model_writes_and_clears(ac, ad, tmp_path):
    writer, home = ac
    assert writer.model_override_info('builder')['model_source'] == 'default'
    writer.set_model_override('builder', 'gpt-5.6', 'copilot')
    assert json.loads((home / 'roles/builder/override.json').read_text()) == {'model': 'gpt-5.6', 'provider': 'copilot'}
    info = writer.model_override_info('builder')
    assert info == {'model': 'gpt-5.6', 'provider': 'copilot', 'model_source': 'override'}
    argv = ad.make('builder', cfg(tmp_path), home).argv('x')
    assert argv[argv.index('-m') + 1] == 'gpt-5.6'
    writer.set_model_override('builder', '')
    assert not (home / 'roles/builder/override.json').exists()


@pytest.mark.parametrize('model,provider', [('--yolo', ''), ('ok', 'bad provider'), ('a;b', ''), ('', 'copilot')])
def test_set_model_rejects(ac, model, provider):
    writer, home = ac
    with pytest.raises(writer.ControlError):
        writer.set_model_override('builder', model, provider)
    assert not (home / 'roles/builder/override.json').exists()


def test_set_model_rejects_non_headless(ac):
    writer, _ = ac
    with pytest.raises(writer.ControlError):
        writer.set_model_override('owner', 'gpt-5.6')


@pytest.mark.parametrize('data', [{'provider': 'copilot'}, {'model': 'gpt-5.6', 'provider': 'bad provider'},
                                  {'model': 'gpt-5.6', 'provider': 5}])
def test_override_rejected_whole_like_writer(ad, tmp_path, data):
    """讀取端與寫入端一致：只有 provider、或 provider 不合法 → 整份不採用（debugger NOTE 2/3）。"""
    home = tmp_path / 'mbox'
    write_over(home, 'builder', data)
    argv = ad.make('builder', cfg(tmp_path), home).argv('x')
    assert '-m' not in argv and '--provider' not in argv


def test_concurrent_writers_no_tmp_collision(ac):
    """debugger NOTE 1：兩個寫入者共用 override.tmp 會 FileNotFoundError；改為各自暫存檔。"""
    import threading
    writer, home = ac
    errors = []
    def worker(i):
        for _ in range(50):
            try:
                writer.set_model_override('builder', f'm{i}')
            except Exception as exc:
                errors.append(exc)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors
    assert json.loads((home / 'roles/builder/override.json').read_text())['model'] in {'m0', 'm1', 'm2', 'm3'}
    assert not list((home / 'roles/builder').glob('*.tmp'))
