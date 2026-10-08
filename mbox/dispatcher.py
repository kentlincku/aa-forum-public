"""dispatcher：讀 roles.json，替每個角色註冊身分；每輪找「有未讀且閒置」的角色並叫醒。

  python -m mbox.dispatcher setup            # 依 roles.json 建身分（已有 token 不換）
  python -m mbox.dispatcher once             # 跑一輪（給 launchd/cron）
  python -m mbox.dispatcher loop [--every 10]
  python -m mbox.dispatcher status
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys as _sys
_sys.path.insert(0, str(ROOT)) if str(ROOT) not in _sys.path else None
from mbox import paths as _paths  # noqa: E402
INST = _paths.instance()
sys.path.insert(0, str(ROOT))

import drivers  # noqa: E402
adapters = drivers  # 相容：既有測試以 dispatcher.adapters 攔截
from mbox.core import Store  # noqa: E402


def home() -> Path:
    return Path(_paths.var())


def load_roles(p: Path) -> dict:
    return json.loads(p.read_text())["roles"]


def setup(roles: dict, store: Store, quiet: bool = False, sync: bool = True):
    say = (lambda *a, **k: None) if quiet else print
    tdir = home() / "tokens"
    tdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    identities = dict(roles)
    identities.setdefault('server', {'runtime': 'system', 'rank': 'human', 'driver': 'manual'})
    for role, cfg in identities.items():
        f = tdir / role
        known = {a["id"] for a in store.agents()}
        if f.exists() and role in known:
            say(f"{role:<10} existing"); continue
        tok = store.add_agent(role, cfg.get("runtime", "generic"), cfg.get("rank", "worker"))
        f.write_text(tok); f.chmod(0o600)
        say(f"{role:<10} created ({drivers.resolve(cfg)})")
    if not sync:
        return
    for role, before, after in store.sync_runtimes(roles):
        say(f"{role:<10} runtime {before} → {after}（依 roles.json）" if after
              else f"{role:<10} roles.json 沒寫 runtime，名冊維持 {before}")


def _set_current_room(role: str, msgs: list):
    """這一輪的信若都來自同一個 AA Forum 群，記下群號：角色這輪用 mbox 派工／私訊時自動帶上（群組範圍＋同步到群）。"""
    rooms = {m.get("source_room") for m in msgs if m.get("source_room") is not None}
    f = home() / "roles" / role / "current_room"
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        if len(rooms) == 1:
            f.write_text(str(rooms.pop()))
        else:
            f.unlink(missing_ok=True)
    except OSError:
        pass


class FailureTracker:
    """Count consecutive failures per operation; alert once per failing streak.

    Counts are local to this dispatcher process and reset after a successful check.
    Guardian notification uses Store.send directly, independently of the stale scan.
    Threshold failures also update MBOX_HOME/dispatcher.alert and stderr without
    using SQLite; only an entirely successful round clears the fallback file.
    """

    def __init__(self, threshold: int = 5):
        if threshold < 1:
            raise ValueError("failure threshold must be at least 1")
        self.threshold = threshold
        self.counts: dict[str, int] = {}
        self.busy_roles: set[str] = set()
        self.pending_errors: list[tuple[str,str,float]] = []

    def success(self, operation: str):
        self.counts.pop(operation, None)

    def failure(self, operation: str, exc: BaseException, store: Store, log):
        count = self.counts.get(operation, 0) + 1
        self.counts[operation] = count
        detail = f"{type(exc).__name__}: {exc}"
        log(f"dispatcher {operation} 失敗 {count} 次：{detail}")
        self.pending_errors.append((operation, detail, time.time()))
        self.flush_errors(store, log)
        if count >= self.threshold:
            body = (f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} "
                    f"!!! DISPATCHER 連續失敗告警：{operation} 已連續失敗 {count} 次；{detail}")
            # Persist and print before attempting mbox: a locked DB cannot block them.
            self.fallback(body, log)
            if count == self.threshold:
                log(body)
                try:
                    # Trusted internal system sender, also used by stale-delivery alerts.
                    store.send({"id": "mbox-dispatcher", "rank": "human"}, "guardian", body,
                               kind="system", idem_key=f"dispatcher-failure-{uuid.uuid4().hex}")
                except Exception as alert_exc:
                    log(f"dispatcher 告警寄送失敗：{type(alert_exc).__name__}: {alert_exc}")

    def flush_errors(self, store: Store, log):
        while self.pending_errors:
            operation, detail, occurred = self.pending_errors[0]
            try:
                store.record_run_error(operation, detail, occurred)
            except Exception as exc:
                log(f'runs.error 暫存待重試：{type(exc).__name__}: {exc}')
                break
            self.pending_errors.pop(0)

    def fallback(self, body: str, log):
        target = home() / "dispatcher.alert"
        temp = target.with_name(f"dispatcher.alert.{uuid.uuid4().hex}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp.write_text(body + "\n", encoding="utf-8")
            temp.chmod(0o600)
            os.replace(temp, target)
        except OSError as exc:
            log(f"dispatcher 備援告警寫檔失敗：{type(exc).__name__}: {exc}")
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError as exc:
                log(f"dispatcher 備援暫存清理失敗：{type(exc).__name__}: {exc}")
        try:
            print(body, file=sys.stderr, flush=True)
        except OSError as exc:
            log(f"dispatcher stderr 告警失敗：{type(exc).__name__}: {exc}")

    def recovered(self, log):
        try:
            (home() / "dispatcher.alert").unlink(missing_ok=True)
        except OSError as exc:
            log(f"dispatcher 備援告警清除失敗：{type(exc).__name__}: {exc}")


def _known_lanes(role: str) -> list[int]:
    d = home() / "roles" / role / "rooms"
    return sorted(int(p.name) for p in d.iterdir() if p.is_dir() and p.name.isdigit()) if d.is_dir() else []


def _idle_hours() -> float:
    try:
        return float(_paths.setting("AAF_SESSION_IDLE_HOURS") or 24)
    except ValueError:
        return 24.0


def _once_lanes(role: str, cfg: dict, roles: dict, store: Store, log, failures, digest_minutes: float):
    """每群一個工作階段（SPEC-1.1 §2）：預設工作階段（不屬於任何群的信）＋每個有信或還開著的群各自叫醒。
    同一角色不同群可並行；同一工作階段內照舊排隊。角色層級的 runs／心跳以「任一工作階段忙＝忙」彙總。"""
    lanes = ["default"] + sorted(set(_known_lanes(role)) | {l for l in store.unread_lanes(role) if l != "default"})
    any_busy = False
    for lane in lanes:
        tag = role if lane == "default" else f"{role}@群{lane}"
        ad = adapters.make(role, cfg, home(), lane=lane)
        ad.sessions = store
        ad.finish()
        tr = ad.take_turn_result()
        if tr:
            try:
                store.record_turn(tr)
            except Exception as exc:
                log(f"{tag}: TurnResult 記錄失敗：{type(exc).__name__}: {exc}")
        state = ad.health()
        if state == "busy":
            any_busy = True
            continue
        if hasattr(ad, "refill_tick"):
            ev = ad.refill_tick()
            if ev:
                log(f"{tag}: {ev}")
                if "失敗" in ev:
                    lead = next((r for r, c in roles.items() if c.get("rank") == "lead"), None)
                    if lead and lead != role:
                        store.send({"id": "mbox-dispatcher", "rank": "human", "runtime": "system"}, lead,
                                   f"[aaf] {tag} {ev}", kind="system")
            if ad._refill_state().get("phase"):
                if ad._refill_state().get("phase") == "park_requested":
                    ev = ad.park_tick(_idle_hours())
                    if ev:
                        log(f"{tag}: {ev}")
                any_busy = any_busy or ad._refill_state().get("phase") is not None
                continue
        n = store.unread_count(role, lane=lane)
        if not n:
            if lane != "default":
                ev = ad.park_tick(_idle_hours())
                if ev:
                    log(f"{tag}: {ev}")
            continue
        if not store.wake_due(role, digest_minutes, lane=lane):
            continue
        msgs = store.dispatch_messages(role, lane=lane)
        head = msgs[0]["id"] if msgs else None
        included_ids = []
        preview = adapters.render_preview(msgs, included_ids)
        r = ad.wake(n, head=head, preview=preview)
        if r.startswith("started") and preview:
            store.begin_run(role, included_ids)
            store.mark_dispatched(role, included_ids, lane=lane)
            any_busy = True
        log(f"{time.strftime('%T')} {tag}: 未讀 {n} → {r}")
    store.observe_role(role, "busy" if any_busy else "idle")
    store.proxy_heartbeat(role, "busy" if any_busy else "idle")
    if not any_busy:
        failures.busy_roles.discard(role)
        store.finish_run(role, None, "")


def once(roles: dict, store: Store, log=print, stale_minutes: float = 5,
         failures: FailureTracker | None = None, digest_minutes: float = 5):
    failures = failures if failures is not None else FailureTracker()
    succeeded = True
    failures.flush_errors(store, log)
    for role, cfg in roles.items():
        operation = f"role:{role}"
        try:
            if drivers.is_manual(cfg):
                store.observe_role(role, 'unknown')
                failures.success(operation)
                continue
            if adapters.supports_lanes(cfg) and not adapters.tmux_room_exists(
                    cfg.get("tmux_session", f"{_paths.tmux_prefix()}{role}")):
                _once_lanes(role, cfg, roles, store, log, failures, digest_minutes)
                failures.success(operation)
                continue
            ad = adapters.make(role, cfg, home())
            if hasattr(ad, "sessions"):
                ad.sessions = store
            # 角色若被手動開了 tmux 互動房（AA Forum 控制台「開房／啟動」），改用 tmux 叫醒，避免同時跑兩個 session
            if getattr(ad, "level", "") == "headless" and adapters.tmux_room_exists(cfg.get("tmux_session", f"{_paths.tmux_prefix()}{role}")):
                ad = drivers.Tmux(role, cfg, home())
            if hasattr(ad, "finish"):
                ad.finish()
            if hasattr(ad, "take_turn_result"):
                tr = ad.take_turn_result()
                if tr:
                    try:
                        store.record_turn(tr)
                    except Exception as exc:  # 契約記錄失敗不影響叫醒流程
                        log(f"{role}: TurnResult 記錄失敗：{type(exc).__name__}: {exc}")
            state = ad.health() if hasattr(ad, 'health') else 'idle'
            store.observe_role(role, state)
            # 代報：輪次進行中 busy；running.pid 消失 → idle。tmux adapter 的 idle 只是「畫面兩次沒變」，
            # 不可靠，只代報 busy（reviewer #42 SHOULD 1）。角色 5 分鐘內自報過則不覆蓋（Store.proxy_heartbeat）。
            if state == 'busy' or (state == 'idle' and getattr(ad, 'level', '') != 'tmux'):
                store.proxy_heartbeat(role, state)
            if state == 'busy':
                store.begin_run(role)
                if role not in failures.busy_roles:
                    log(f'{role}: busy（本次狀態只記一次）')
                    failures.busy_roles.add(role)
                failures.success(operation)
                continue
            failures.busy_roles.discard(role)
            if state == 'idle' and hasattr(ad, 'refill_tick'):
                ev = ad.refill_tick()
                if ev:
                    log(f"{role}: {ev}")
                    if "失敗" in ev:
                        lead = next((r for r, c in roles.items() if c.get("rank") == "lead"), None)
                        if lead and lead != role:
                            store.send({"id": "mbox-dispatcher", "rank": "human", "runtime": "system"}, lead,
                                       f"[aaf] {role} {ev}", kind="system")
                if ad._refill_state().get("phase"):
                    store.observe_role(role, 'busy')
                    failures.success(operation)
                    continue
            if state == 'idle':
                exit_code, error = ad.completion_status() if hasattr(ad, 'completion_status') else (None, '')
                store.finish_run(role, exit_code, error)
            n = store.unread_count(role)
            if n and store.wake_due(role, digest_minutes):
                msgs = store.dispatch_messages(role)
                head = msgs[0]["id"] if msgs else None
                included_ids = []
                preview = adapters.render_preview(msgs, included_ids) if getattr(ad, "level", "") == "headless" else ""
                _set_current_room(role, msgs)
                r = ad.wake(n, head=head, preview=preview)
                if r.startswith("started") and preview:
                    # 內容已隨提示送出 → 標為 delivered（仍要 agent 自己 ack done）
                    store.begin_run(role, included_ids)
                    store.mark_dispatched(role, included_ids)
                    store.observe_role(role, 'busy')
                    store.proxy_heartbeat(role, 'busy')
                elif r.startswith('sent'):
                    store.begin_run(role, [m['id'] for m in msgs])
                    store.mark_dispatched(role, [], delivered=False)
                    store.proxy_heartbeat(role, 'busy')
                log(f"{time.strftime('%T')} {role}: 未讀 {n} → {r}")
        except (Exception, SystemExit) as exc:
            # Invalid adapter configuration raises SystemExit; isolate it too.
            # KeyboardInterrupt is deliberately allowed to stop the dispatcher.
            succeeded = False
            try:
                store.observe_role(role, 'unknown')
            except Exception as timing_exc:
                log(f'角色計時暫停失敗：{type(timing_exc).__name__}: {timing_exc}')
            failures.failure(operation, exc, store, log)
        else:
            failures.success(operation)
    try:
        alerts = store.alert_must_deliveries(stale_minutes)
        if alerts:
            log(f"未 ack 老化告警：已通知 guardian {alerts} 則")
    except Exception as exc:
        succeeded = False
        failures.failure("alert_must_deliveries", exc, store, log)
    else:
        failures.success("alert_must_deliveries")
    try:
        mismatches = store.alert_priority_mismatches()
        if mismatches:
            log(f'通知漏判：已通知 guardian {mismatches} 則')
    except Exception as exc:
        succeeded = False
        failures.failure('alert_priority_mismatches', exc, store, log)
    else:
        failures.success('alert_priority_mismatches')
    if succeeded:
        failures.recovered(log)
    return succeeded


def heartbeat(log=print):
    """Refresh the process liveness file after each round, independently of SQLite."""
    try:
        target = home() / "dispatcher.heartbeat"
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_text(f"time={time.strftime('%Y-%m-%dT%H:%M:%S%z')} pid={os.getpid()}\n",
                          encoding="utf-8")
    except OSError as exc:
        log(f"dispatcher heartbeat 寫檔失敗：{type(exc).__name__}: {exc}")


def drain_retiring(watcher, store: Store, log=print):
    """已從 roles.json 移除、但上一輪還在跑的角色：不叫醒，只等它跑完並收尾（記 TurnResult）。"""
    for role, cfg in watcher.retiring_roles().items():
        try:
            ad = adapters.make(role, cfg, home())
            if hasattr(ad, "sessions"):
                ad.sessions = store
            if hasattr(ad, "finish"):
                ad.finish()
            tr = ad.take_turn_result() if hasattr(ad, "take_turn_result") else None
            if tr:
                store.record_turn(tr)
            if ad.health() != "busy":
                exit_code, error = ad.completion_status() if hasattr(ad, "completion_status") else (None, "")
                store.finish_run(role, exit_code, error)
                watcher.retired(role)
                log(f"{role}: 已移除的角色最後一輪已收尾")
        except (Exception, SystemExit) as exc:
            log(f"{role}: 收尾失敗：{type(exc).__name__}: {exc}")
            watcher.retired(role)


def loop(roles: dict, store: Store, every: float = 10, stale_minutes: float = 5,
         failure_threshold: int = 5, log=print, digest_minutes: float = 5,
         roles_path: Path | None = None, doctor=None):
    failures = FailureTracker(failure_threshold)
    watcher = None
    if roles_path is not None:
        from mbox.roster import RosterWatcher
        from mbox import doctor as _doctor
        watcher = RosterWatcher(roles_path, roles, home(), alert=_doctor.enqueue, log=log)
    try:
        # 啟動時補發 token：dispatcher 停機期間加進 roles.json 的角色，熱重載看不到（啟動快照已含它）
        known = {a["id"] for a in store.agents()}
        missing = {r: c for r, c in roles.items() if r not in known}   # 只補名冊裡完全沒有的角色
        if missing:
            setup(missing, store, quiet=True, sync=False)
            log("啟動時補建角色：" + "、".join(missing))
    except Exception as exc:
        log(f"啟動時 setup 失敗：{type(exc).__name__}: {exc}")
    try:
        for role, before, after in store.sync_runtimes(roles):
            log(f"{role}: 名冊 runtime {before} → {after}（依 roles.json）" if after
                else f"{role}: roles.json 沒寫 runtime，名冊維持 {before}")
    except Exception as exc:
        failures.failure("sync_runtimes", exc, store, log)
    while True:
        if watcher is not None:
            try:
                watcher.poll(store)
                watcher.retry_zk()
                roles = watcher.roles
                drain_retiring(watcher, store, log)
            except Exception as exc:
                failures.failure("roster", exc, store, log)
        try:
            succeeded = once(roles, store, log=log, stale_minutes=stale_minutes, failures=failures, digest_minutes=digest_minutes)
        except (Exception, SystemExit) as exc:
            failures.failure("loop", exc, store, log)
        else:
            failures.success("loop")
            if succeeded:
                failures.recovered(log)
        finally:
            heartbeat(log)
        if doctor is not None:
            try:
                doctor.tick(store, roles, log)
            except Exception as exc:
                log(f"doctor 失敗：{type(exc).__name__}: {exc}")
        time.sleep(every)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mbox-dispatcher")
    ap.add_argument("cmd", choices=["setup", "once", "loop", "status"])
    ap.add_argument("--roles", type=Path, default=Path(_paths.roles_file()))
    ap.add_argument("--every", type=float, default=10)
    ap.add_argument("--stale-minutes", type=float,
                    default=float(os.environ.get("MBOX_MUST_MINUTES", "5")),
                    help="must 已送達且 idle 未 ack 告警分鐘數（預設 5；<=0 停用）")
    ap.add_argument("--failure-threshold", type=int,
                    default=int(os.environ.get("MBOX_FAILURE_THRESHOLD", "5")),
                    help="同一操作連續失敗 N 次告警（預設 5；至少 1）")
    ap.add_argument("--no-doctor", action="store_true", help="不跑自我檢查與告警")
    ap.add_argument('--digest-minutes', type=float, default=float(os.environ.get('MBOX_DIGEST_MINUTES', '5')))
    a = ap.parse_args(argv)
    if a.failure_threshold < 1:
        ap.error("--failure-threshold must be at least 1")
    home().mkdir(mode=0o700, parents=True, exist_ok=True)
    store = Store(home() / "mbox.sqlite3")
    roles = load_roles(a.roles)
    if a.cmd == "setup":
        setup(roles, store)
    elif a.cmd == "once":
        once(roles, store, stale_minutes=a.stale_minutes, failures=FailureTracker(a.failure_threshold), digest_minutes=a.digest_minutes)
    elif a.cmd == "loop":
        print(f"dispatcher loop every {a.every}s", flush=True)
        from mbox.doctor import Doctor
        loop(roles, store, every=a.every, stale_minutes=a.stale_minutes,
             failure_threshold=a.failure_threshold, digest_minutes=a.digest_minutes, log=lambda s: print(s, flush=True),
             roles_path=a.roles, doctor=None if a.no_doctor else Doctor())
    else:
        for role, cfg in roles.items():
            ad = adapters.make(role, cfg, home())
            print(f"{role:<10} {drivers.resolve(cfg):<16} {ad.health():<8} 未讀 {store.unread_count(role)}")


if __name__ == "__main__":
    main()
