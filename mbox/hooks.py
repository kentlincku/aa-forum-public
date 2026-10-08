"""通用檢查掛點（P4 / D5）：發信、派工、發群文前後跑訂製規則。對所有 runtime 一視同仁。

規則放在 $AAF_HOOKS 目錄（預設 <專案>/hooks；訂製版指向自己的 hooks/）。每個 *.py 可定義：

  def pre(event: dict) -> str | None
      回字串＝擋下（字串就是給發文者看的原因）；回 None＝放行。
  def post(event: dict) -> str | None
      已送出之後才跑，不擋。回字串＝提醒，下次叫醒該角色時附在 wake prompt 最前面（一次後清除）。

event 欄位：
  type        "mbox.send" | "mbox.task" | "chat.post"
  sender      發送者 id（AA Forum Owner 會換成使用者 id）
  rank        發送者 rank（worker/lead/human/system，AA Forum 端不一定有）
  to          收件者（mbox）或 None
  to_rank     收件者 rank（mbox 單一收件者時）
  room        群號（chat.post 或帶群的 mbox）或 None
  kind        mbox kind／AA Forum notification_kind
  body        內文
  reply_to    回覆對象或 None
  argv        （保留）

  def wake(event: dict) -> str | None
      每次叫醒角色前呼叫（相當於互動 CLI 的「送出提示前」hook）。回字串＝附在這一輪提示最前面。
      event：type="wake"、role、room（群工作階段的群號或 None）、unread、session_new（是否新 session）。
  def turn_end(event: dict) -> str | None
      每一輪結束後呼叫（相當於「Stop」hook）。回字串＝下次叫醒時附上的提醒。
      event：type="turn_end"、role、room、ok、error、text（本輪回覆尾段）。

原則：
- fail-open：規則本身崩潰時放行，錯誤寫進 $MBOX_HOME/hooks.log；不能因為規則寫壞而讓整個團隊無法說話。
- 系統身分（runtime=system、mbox-dispatcher）的訊息不跑規則。
- 以檔名排序執行；第一個擋下的規則生效。
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def hooks_dir() -> Path:
    from mbox import paths
    return Path(os.environ.get("AAF_HOOKS") or paths.instance() / "hooks")


def _home() -> Path:
    from mbox import paths
    return paths.var()


_cache: dict[str, tuple[float, object]] = {}


def _rules():
    d = hooks_dir()
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.py")):
        if f.name.startswith("_"):
            continue
        mt = f.stat().st_mtime
        hit = _cache.get(str(f))
        if hit and hit[0] == mt:
            out.append(hit[1]); continue
        try:
            spec = importlib.util.spec_from_file_location(f"civ_hook_{f.stem}", f)
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception:
            _log(f"載入失敗 {f.name}\n{traceback.format_exc()}")
            continue
        _cache[str(f)] = (mt, mod)
        out.append(mod)
    return out


def _log(text: str):
    try:
        with open(_home() / "hooks.log", "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%F %T ") + text.rstrip() + "\n")
    except OSError:
        pass


def _is_system(event: dict) -> bool:
    return event.get("rank") == "system" or event.get("sender") in ("mbox-dispatcher", "server", "__system__")


def run_pre(event: dict) -> str | None:
    """回擋下原因或 None。"""
    if _is_system(event):
        return None
    for mod in _rules():
        fn = getattr(mod, "pre", None)
        if not fn:
            continue
        try:
            reason = fn(dict(event))
        except Exception:
            _log(f"pre 例外（放行）{getattr(mod, '__name__', '?')}\n{traceback.format_exc()}")
            continue
        if reason:
            _log(f"擋下 {event.get('type')} {event.get('sender')}→{event.get('to') or event.get('room')}："
                 f"{getattr(mod, '__name__', '?')}：{str(reason)[:200]}")
            return str(reason)
    return None


def run_post(event: dict) -> list[str]:
    if _is_system(event):
        return []
    notes = []
    for mod in _rules():
        fn = getattr(mod, "post", None)
        if not fn:
            continue
        try:
            note = fn(dict(event))
        except Exception:
            _log(f"post 例外（忽略）{getattr(mod, '__name__', '?')}\n{traceback.format_exc()}")
            continue
        if note:
            notes.append(str(note))
    if notes and event.get("sender"):
        add_notes(event["sender"], notes)
    return notes


def _notes_file(agent: str, home: Path | None = None) -> Path:
    return (home or _home()) / "roles" / agent / "hook_notes.jsonl"


def add_notes(agent: str, notes: list[str]):
    f = _notes_file(agent)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            for n in notes:
                fh.write(json.dumps({"at": time.time(), "note": n}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def take_notes(agent: str, home: Path | None = None) -> list[str]:
    """取出並清除該角色待附的提醒（同內容只留一次）。"""
    f = _notes_file(agent, home)
    if not f.exists():
        return []
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
        f.unlink()
    except OSError:
        return []
    seen, out = set(), []
    for l in lines:
        try:
            n = json.loads(l)["note"]
        except (ValueError, KeyError):
            continue
        if n not in seen:
            seen.add(n); out.append(n)
    return out


def run_wake(event: dict) -> list[str]:
    """叫醒前：收集各規則要附在提示前面的文字（fail-open）。"""
    out = []
    for mod in _rules():
        fn = getattr(mod, "wake", None)
        if not fn:
            continue
        try:
            t = fn(dict(event))
        except Exception:
            _log(f"wake 例外（略過）{getattr(mod, '__name__', '?')}\n{traceback.format_exc()}")
            continue
        if t:
            out.append(str(t))
    return out


def run_turn_end(event: dict) -> None:
    """一輪結束後：規則回的提醒存進 hook_notes，下次叫醒附上（fail-open）。"""
    notes = []
    for mod in _rules():
        fn = getattr(mod, "turn_end", None)
        if not fn:
            continue
        try:
            t = fn(dict(event))
        except Exception:
            _log(f"turn_end 例外（略過）{getattr(mod, '__name__', '?')}\n{traceback.format_exc()}")
            continue
        if t:
            notes.append(str(t))
    if notes and event.get("role"):
        add_notes(event["role"], notes)
