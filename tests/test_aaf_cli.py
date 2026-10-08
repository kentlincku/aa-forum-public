"""bin/aaf：依 pid 檔管理單例、up/down/restart、supervisor（以假服務測，不碰 live）。"""
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAKEPY = r'''#!/bin/bash
# 假 python：服務型參數就常駐，其他（dispatcher setup/status）立即結束。
# 用 exec -a 讓 ps 的 argv[0] 正好是本檔路徑（同真 python），aaf 的 argv[0]==$PY 檢查才成立。
case "$*" in
  *"-m mbox.broker"*|*"server/app.py"*|*"-m mbox.dispatcher loop"*) ;;
  *) exit 0 ;;
esac
exec -a "$0" "$FAKE_BASH" -c '
if [ -f "$FAKE_ROOT/ignore_term" ]; then trap "" TERM; else trap "exit 0" TERM; fi
case "$*" in
  *"mbox.dispatcher loop"*)
    # 祖先反例：由服務的子孫程序呼叫 aaf down
    if [ -f "$FAKE_ROOT/stop_from_child" ]; then
      ( "$FAKE_ROOT/bin/aaf" down > "$FAKE_ROOT/child_stop.out" 2>&1; echo done >> "$FAKE_ROOT/child_stop.out" ) &
    fi ;;
esac
while :; do sleep 0.1; done
' "$0" "$@"
'''


@pytest.fixture()
def env(tmp_path):
    root = (tmp_path / 'civ').resolve()
    (root / 'bin').mkdir(parents=True)
    shutil.copy(ROOT / 'bin' / 'aaf', root / 'bin' / 'aaf')
    shutil.copy(ROOT / 'bin' / '_instance_env.sh', root / 'bin' / '_instance_env.sh')
    fake = root / 'fakepy'
    fake.write_text(FAKEPY)
    fake.chmod(0o755)
    # macOS 不讓 ps eww 讀 Apple 平台二進位（/bin/bash）的環境變數；真服務是 .venv 的 python，讀得到。
    # 測試的假服務改用一份重新簽章的 bash，AAF_HOME 才看得到（Linux 讀 /proc，不受影響）。
    fake_bash = root / 'bash'
    shutil.copy('/bin/bash', fake_bash)
    if sys.platform == 'darwin':
        subprocess.run(['codesign', '-s', '-', '-f', str(fake_bash)], capture_output=True)
    e = {k: v for k, v in os.environ.items() if not k.startswith(('MBOX_', 'AAF_', 'AAF_', 'AAF_'))}
    e.update(FAKE_BASH=str(fake_bash), AAF_UP_PY=str(fake), AAF_UP_STOP_WAIT='2', AAF_UP_NO_HTTP='1', FAKE_ROOT=str(root),
             AAF_SUP_EVERY='0.3', AAF_SUP_WINDOW='30', AAF_SUP_MAX='3')
    yield root, e
    # 收尾：殺掉所有假服務
    out = subprocess.run(['ps', '-U', str(os.getuid()), '-o', 'pid=,command='], capture_output=True, text=True).stdout
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(' ')
        if str(fake) in cmd:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except OSError:
                pass


def up(env, *args, timeout=30):
    root, e = env
    return subprocess.run([str(root / 'bin' / 'aaf'), *args], capture_output=True, text=True,
                          env=e, timeout=timeout)


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # macOS 殭屍程序 kill 0 仍成功：以 ps 狀態排除
    st = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith('Z')


def pids(root):
    return {n: int((root / 'var' / f'{n}.pid').read_text()) for n in ('broker', 'server', 'dispatcher')
            if (root / 'var' / f'{n}.pid').exists()}


def test_start_idempotent_and_stop(env):
    root, _ = env
    r = up(env, 'up')
    assert r.returncode == 0, r.stderr
    first = pids(root)
    assert set(first) == {'broker', 'server', 'dispatcher'} and all(alive(p) for p in first.values())
    r = up(env, 'up')
    assert r.stdout.count('已在執行') == 4 and 'supervisor：已在執行' in r.stdout and pids(root) == first
    st = up(env, 'status').stdout
    assert all(f'pid {p}' in st for p in first.values())
    r = up(env, 'down')
    assert r.returncode == 0, r.stdout + r.stderr
    time.sleep(0.3)
    assert not any(alive(p) for p in first.values()) and pids(root) == {}


def test_restart_single_service(env):
    root, _ = env
    up(env, 'up')
    before = pids(root)
    r = up(env, 'restart', 'server')
    assert r.returncode == 0, r.stderr
    after = pids(root)
    assert after['broker'] == before['broker'] and after['dispatcher'] == before['dispatcher']
    assert after['server'] != before['server'] and alive(after['server'])
    assert not alive(before['server'])
    assert up(env, 'restart', 'nope').returncode == 2
    up(env, 'down')


def test_stale_pid_file_dead_pid(env):
    root, _ = env
    (root / 'var').mkdir()
    dead = subprocess.Popen(['true'])
    dead.wait()
    (root / 'var' / 'broker.pid').write_text(str(dead.pid))
    r = up(env, 'up')
    assert '已啟動' in r.stdout.split('\n')[0]
    assert pids(root)['broker'] != dead.pid and alive(pids(root)['broker'])
    up(env, 'down')


def test_stale_pid_file_reused_by_other_program(env):
    """pid 檔指向別的程式（pid 重用）：不當成服務、不殺它。"""
    root, _ = env
    (root / 'var').mkdir()
    other = subprocess.Popen(['sleep', '30'])
    try:
        (root / 'var' / 'dispatcher.pid').write_text(str(other.pid))
        up(env, 'up')
        assert pids(root)['dispatcher'] != other.pid
        up(env, 'down')
        assert alive(other.pid)
    finally:
        other.kill()
        other.wait()


def test_adopt_service_without_pid_file(env):
    """舊版啟動（無 pid 檔）的服務：以 ps 掃描收編，不重開第二個。"""
    root, e = env
    proc = subprocess.Popen([e['AAF_UP_PY'], '-m', 'mbox.dispatcher', 'loop', '--every', '10'], env=e,
                            cwd=root)
    try:
        time.sleep(0.3)
        r = up(env, 'up')
        assert f'dispatcher：已在執行（pid {proc.pid}）' in r.stdout
        up(env, 'down')
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_stop_escalates_to_kill(env):
    root, _ = env
    (root / 'ignore_term').write_text('1')
    up(env, 'up')
    p = pids(root)
    r = up(env, 'down')
    assert r.returncode == 0 and 'KILL' in r.stdout
    time.sleep(0.3)
    assert not any(alive(x) for x in p.values())


def test_stop_from_descendant_of_managed_service(env):
    """pgrep 祖先排除的反例：從 dispatcher 的子孫執行 stop，仍能停掉 dispatcher 本身。"""
    root, _ = env
    (root / 'stop_from_child').write_text('1')
    up(env, 'up')
    p = pids(root)
    out = root / 'child_stop.out'
    deadline = time.time() + 20
    while time.time() < deadline and not (out.exists() and 'done' in out.read_text()):
        time.sleep(0.2)
    text = out.read_text()
    assert f'dispatcher：已停止（pid {p["dispatcher"]}）' in text, text
    time.sleep(0.3)
    assert not any(alive(x) for x in p.values())
    # 對照：macOS pgrep 從同位置會看不到祖先（說明舊版為何失敗）——此處不重跑，只記於 record。


def spawn_dispatcher(e, cwd):
    return subprocess.Popen([e['AAF_UP_PY'], '-m', 'mbox.dispatcher', 'loop', '--every', '10'], env=e, cwd=cwd)


def test_two_dispatchers_without_pid_file_not_adopted(env):
    """reviewer #42 MUST 反例：沒有 pid 檔、有兩個 dispatcher → 不收編、不停，列清單並 exit 非 0。"""
    root, e = env
    (root / 'var').mkdir()
    a, b = spawn_dispatcher(e, root), spawn_dispatcher(e, root)
    try:
        time.sleep(0.4)
        r = up(env, 'down')
        assert r.returncode != 0
        assert '找到 2 個符合的程序' in r.stderr and str(a.pid) in r.stderr and str(b.pid) in r.stderr
        assert f'kill {a.pid}' in r.stderr and f'kill {b.pid}' in r.stderr and 'var/dispatcher.pid' in r.stderr
        assert 'README' not in r.stderr
        assert alive(a.pid) and alive(b.pid) and not (root / 'var' / 'dispatcher.pid').exists()
        r = up(env, 'up')
        assert r.returncode != 0 and pids(root).get('dispatcher') is None  # 不另開第三個
        assert up(env, 'status').returncode != 0
    finally:
        for p in (a, b):
            p.kill(); p.wait()
        up(env, 'down')


def test_sandbox_dispatcher_other_cwd_ignored(env, tmp_path):
    """reviewer #42 MUST 反例：同一個 $PY、但 cwd 不是 ROOT 的沙箱 dispatcher → 不視為本服務。"""
    root, e = env
    sandbox = tmp_path / 'sandbox'
    sandbox.mkdir()
    other = spawn_dispatcher(e, sandbox)
    try:
        time.sleep(0.4)
        r = up(env, 'up')
        assert r.returncode == 0 and pids(root)['dispatcher'] != other.pid
        up(env, 'down')
        assert alive(other.pid)
    finally:
        other.kill(); other.wait()


def test_shell_wrapper_not_adopted(env):
    """argv[0] 不是 $PY 的包裝程序（bash -c "… $PY -m mbox.broker …"）不收編。"""
    root, e = env
    wrapper = subprocess.Popen(['/bin/bash', '-c', f'sleep 30; : {e["AAF_UP_PY"]} -m mbox.broker'], cwd=root)
    try:
        time.sleep(0.3)
        up(env, 'up')
        assert pids(root)['broker'] != wrapper.pid
        up(env, 'down')
        assert alive(wrapper.pid)
    finally:
        wrapper.kill(); wrapper.wait()


def sup_pid(root):
    f = root / 'var' / 'supervisor.pid'
    return int(f.read_text()) if f.exists() else None


def wait_for(cond, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.1)
    return False


def test_supervisor_restarts_killed_service(env):
    root, _ = env
    assert up(env, 'up').returncode == 0
    s = sup_pid(root)
    assert s and alive(s)
    old = pids(root)['server']
    os.kill(old, signal.SIGKILL)
    assert wait_for(lambda: pids(root).get('server') not in (None, old) and alive(pids(root)['server']))
    assert '不在，重開' in (root / 'var' / 'supervisor.log').read_text()
    up(env, 'down')


def test_down_is_not_pulled_back(env):
    root, _ = env
    up(env, 'up')
    s = sup_pid(root)
    p = pids(root)
    r = up(env, 'down')
    assert r.returncode == 0 and 'supervisor：已停止' in r.stdout
    time.sleep(1.5)  # 超過數個 supervisor 週期
    assert not alive(s) and not any(alive(x) for x in p.values()) and pids(root) == {}


def test_supervisor_gives_up_after_limit_and_alerts(env):
    """服務一直起不來：5 分鐘視窗內重開 3 次後停止並寫告警佇列。"""
    root, e = env
    up(env, 'up')
    # 讓 broker 一啟動就死：假 python 遇到 broker 參數直接退出
    fake = Path(e['AAF_UP_PY'])
    fake.write_text(fake.read_text().replace('*"-m mbox.broker"*|', ''))
    os.kill(pids(root)['broker'], signal.SIGKILL)
    ok = wait_for(lambda: (root / 'var' / 'supervisor.gaveup.broker').exists(), timeout=45)   # 全套測試同時跑時機器較忙
    assert ok, (root / 'var' / 'supervisor.log').read_text()[-3000:]
    q = (root / 'var' / 'alerts' / 'queue.jsonl').read_text()
    assert 'service:broker' in q and '已停止自動重開' in q
    st = up(env, 'status').stdout
    assert '已停止重開 broker' in st
    # 其他服務照常被顧
    assert alive(pids(root)['dispatcher'])
    up(env, 'down')


def test_up_adopts_running_services_after_supervisor_death(env):
    root, _ = env
    up(env, 'up')
    first = pids(root)
    os.kill(sup_pid(root), signal.SIGKILL)
    assert '未執行：supervisor' in up(env, 'status').stdout
    r = up(env, 'up')
    assert r.stdout.count('已在執行') == 3 and 'supervisor：已啟動' in r.stdout and pids(root) == first
    up(env, 'down')


def test_logs_and_usage(env):
    root, _ = env
    up(env, 'up')
    assert wait_for(lambda: 'supervisor 啟動' in (root / 'var' / 'supervisor.log').read_text())
    r = up(env, 'logs', 'supervisor', '-n', '5')
    assert 'supervisor 啟動' in r.stdout
    assert up(env, 'bogus').returncode == 2 and up(env, 'help').returncode == 0
    up(env, 'down')


def test_stale_heartbeat_from_previous_run_does_not_kill_fresh_dispatcher(env):
    """上次留下的舊 heartbeat 檔不能讓剛啟動的 dispatcher 被當成卡死（實跑發現的 bug）。"""
    root, e = env
    (root / 'var').mkdir(exist_ok=True)
    hb = root / 'var' / 'dispatcher.heartbeat'
    hb.write_text('old')
    os.utime(hb, (time.time() - 3600, time.time() - 3600))
    e = dict(e, AAF_SUP_HB_STALE='2')
    subprocess.run([str(root / 'bin' / 'aaf'), 'up'], env=e, capture_output=True, text=True, timeout=30)
    first = pids(root)['dispatcher']
    time.sleep(1.2)
    assert pids(root)['dispatcher'] == first and '卡死' not in (root / 'var' / 'supervisor.log').read_text()
    # 但真的超過門檻仍沒有新 heartbeat → 判定卡死並重開
    assert wait_for(lambda: '卡死' in (root / 'var' / 'supervisor.log').read_text(), timeout=10)
    assert wait_for(lambda: pids(root).get('dispatcher') not in (None, first))
    subprocess.run([str(root / 'bin' / 'aaf'), 'down'], env=e, capture_output=True, timeout=30)


def test_loop_start_issues_tokens_for_roles_added_while_down(tmp_path, monkeypatch):
    """dispatcher 停機時加入 roles.json 的角色：重啟後要有 token（先前只有 bin/aaf up 會補）。"""
    import mbox.dispatcher as d
    from mbox.core import Store
    monkeypatch.setenv("MBOX_HOME", str(tmp_path))
    st = Store(tmp_path / "mbox.sqlite3")
    roles = {"newbie": {"driver": "manual", "rank": "worker"}}
    calls = []
    monkeypatch.setattr(d, "once", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        d.loop(roles, st, every=0.01, log=lambda s: None)
    except KeyboardInterrupt:
        pass
    assert (tmp_path / "tokens" / "newbie").exists()


def test_other_instance_service_ignored(env, tmp_path):
    """同一份公版跑兩個實例：別的實例（AAF_HOME 不同）的 dispatcher 不收編、不停、supervisor 不碰。
    實測事故：暫存實例的 supervisor 把另一個實例的 dispatcher 當成自己的，判定 heartbeat 逾時而重開。"""
    root, e = env
    other_home = tmp_path / 'other-instance'
    other_home.mkdir()
    other = spawn_dispatcher({**e, 'AAF_HOME': str(other_home)}, root)
    try:
        time.sleep(0.4)
        r = up(env, 'up')
        assert r.returncode == 0, r.stderr
        assert pids(root)['dispatcher'] != other.pid
        up(env, 'down')
        assert alive(other.pid)
    finally:
        other.kill(); other.wait()
