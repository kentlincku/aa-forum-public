"""Regression for unconsumed output when a role switches runtime."""
import json
import subprocess
import sys

import pytest

import adapters


@pytest.mark.parametrize('filename', ['last_out.txt', 'last_out.hermes.txt'])
def test_switch_before_previous_output_consumed_does_not_import_foreign_session(tmp_path, monkeypatch, filename):
    monkeypatch.setattr(adapters, 'system_prompt', lambda *args: 'synthetic prompt')
    cfg = {'workdir': str(tmp_path / 'work')}
    old = adapters.HermesHeadless('role', dict(cfg, runtime='hermes'), tmp_path)
    foreign = 'synthetic-hermes-previous-session'
    output = old.state_dir / filename
    output.write_text(json.dumps({'type': 'result', 'session_id': foreign, 'exit_code': 0}) + '\n')
    new = adapters.CodexHeadless('role', dict(cfg, runtime='codex'), tmp_path)
    new.finish()
    assert new.session() is None
    assert foreign not in new.argv('prompt')
    assert output.exists()
    if filename != 'last_out.txt':
        old.finish()
        assert old.session() == foreign
        assert foreign in old.argv('prompt')
        assert not output.exists()


@pytest.mark.parametrize('runtime', ['hermes', None])
def test_wake_output_is_consumed_by_same_runtime_only(tmp_path, monkeypatch, runtime):
    cfg = {'workdir': str(tmp_path / 'work')}
    if runtime:
        cfg['runtime'] = runtime
    adapter = adapters.HermesHeadless('role', cfg, tmp_path)
    sid = 'synthetic-own-session'
    payload = json.dumps({'type': 'result', 'session_id': sid, 'exit_code': 0})
    monkeypatch.setattr(adapter, 'argv', lambda prompt: [sys.executable, '-c', f'print({payload!r})'])
    processes = []
    popen = subprocess.Popen

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(adapters.subprocess, 'Popen', launch)
    assert adapter.wake(1, head=1).startswith('started pid=')
    assert processes[0].wait(timeout=10) == 0
    suffix = runtime or 'HermesHeadless'
    output = adapter.state_dir / f'last_out.{suffix}.txt'
    assert output.exists()
    adapter.finish()
    assert adapter.session() == sid
    assert not output.exists()
    assert adapter.completion_status() == (0, '')
    reloaded = adapters.HermesHeadless('role', cfg, tmp_path)
    assert reloaded.session() == sid
    monkeypatch.setattr(adapters, 'system_prompt', lambda *args: 'synthetic prompt')
    assert sid in reloaded.argv('prompt')
