"""群聊逐字稿匯出（SPEC-1.0 §3）。

每個群一個資料夾：<輸出根>/<群號>/<YYYY-MM-DD>.md，加 _index.md（每天的則數與編號範圍）。
- 內容：留言（含機械提醒）與系統公告；按讚、已讀不放（S7）。附件只列檔名與大小，不複製檔案。
- 冪等：同一天整份重寫，內容沒變就不寫（mtime 不動）。
- 範圍：使用中與已凍結的群；已刪除的群不匯出。
- 時區：AAF_TZ（IANA 名稱），預設系統時區。
由 AA Forum 程序的背景工作定期呼叫（S6），間隔 AAF_TRANSCRIPT_MINUTES（預設 120，0＝停用）。
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


def _tz(name: str | None):
    if name and ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.0f} KB" if n < 1048576 else f"{n / 1048576:.1f} MB"


def _write_if_changed(path: Path, text: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def export(db: sqlite3.Connection, out_root: Path, tz_name: str | None = None,
           rooms: list[int] | None = None, since: str | None = None) -> dict:
    """匯出逐字稿；回傳 {'written': [路徑...], 'rooms': n}。db 需 row_factory=sqlite3.Row。"""
    tz = _tz(tz_name)
    labels = {r["id"]: r["label"] for r in db.execute("SELECT id,label FROM agents")}
    q = "SELECT id,name,state FROM rooms WHERE COALESCE(state,'active')!='deleted'"
    room_rows = [r for r in db.execute(q) if rooms is None or r["id"] in rooms]
    written: list[str] = []
    for room in room_rows:
        rid = room["id"]
        members = [labels.get(r["agent"], r["agent"]) for r in
                   db.execute("SELECT agent FROM members WHERE room=? ORDER BY agent", (rid,))]
        msgs = db.execute("""
            SELECT m.id,m.author,m.body,m.reply_to,m.created,
                   (SELECT 1 FROM system_announcements s WHERE s.message=m.id) AS sys,
                   f.name AS fname, f.size AS fsize, (i.message IS NOT NULL) AS img
            FROM messages m LEFT JOIN files f ON f.message=m.id LEFT JOIN images i ON i.message=m.id
            WHERE m.room=? ORDER BY m.id""", (rid,)).fetchall()
        days: dict[str, list] = {}
        for m in msgs:
            day = datetime.fromtimestamp(m["created"], tz).strftime("%Y-%m-%d")
            if since and day < since:
                continue
            days.setdefault(day, []).append(m)
        d = out_root / str(rid)
        index_rows = []
        for day, items in sorted(days.items()):
            lines = [f"# 群 {rid}「{room['name']}」逐字稿 · {day}", "",
                     f"- 成員：{'、'.join(members)}",
                     f"- 時區：{getattr(tz, 'key', None) or str(tz)}（UTC{datetime.now(tz).strftime('%z')[:3]}:{datetime.now(tz).strftime('%z')[3:]}）",
                     f"- 本檔：{len(items)} 則，#{items[0]['id']}–#{items[-1]['id']}",
                     f"- 狀態：{'已凍結' if room['state'] == 'frozen' else '使用中'}",
                     "- 引用寫「群 %d #編號」；內文為原文，未改寫。" % rid, ""]
            for m in items:
                who = labels.get(m["author"], m["author"])   # 角色的 label 已含 id（例「組長 lead」）
                hhmm = datetime.fromtimestamp(m["created"], tz).strftime("%H:%M:%S")
                head = f"### #{m['id']} · {hhmm} · {'系統公告' if m['sys'] else who}"
                if m["reply_to"]:
                    head += f" ↩ #{m['reply_to']}"
                lines.append(head)
                if m["body"]:
                    lines.append(m["body"])
                if m["fname"]:
                    lines.append(f"📎 {m['fname']}（{_size(m['fsize'])}）")
                if m["img"]:
                    lines.append("🖼 圖片")
                lines.append("")
            if _write_if_changed(d / f"{day}.md", "\n".join(lines)):
                written.append(str(d / f"{day}.md"))
            index_rows.append(f"| {day} | {len(items)} | #{items[0]['id']}–#{items[-1]['id']} |")
        if days and not since:
            idx = "\n".join([f"# 群 {rid}「{room['name']}」逐字稿索引", "",
                             "| 日期 | 則數 | 編號範圍 |", "|---|---|---|", *index_rows, ""])
            if _write_if_changed(d / "_index.md", idx):
                written.append(str(d / "_index.md"))
    return {"written": written, "rooms": len(room_rows)}


class Scheduler:
    """AA Forum 背景工作每 tick 呼叫 due()；到時才匯出。minutes<=0 停用。"""

    def __init__(self, minutes: float):
        self.minutes = minutes
        self.last = 0.0

    def due(self, now: float | None = None) -> bool:
        if self.minutes <= 0:
            return False
        now = now or time.time()
        if now - self.last >= self.minutes * 60:
            self.last = now
            return True
        return False


def _cli(argv=None):
    """bin/aaf transcript [--room N]... [--since YYYY-MM-DD]：立即匯出（不必等排程）。"""
    import argparse
    import contextlib
    import sys
    ap = argparse.ArgumentParser(prog="aaf transcript")
    ap.add_argument("--room", type=int, action="append")
    ap.add_argument("--since")
    a = ap.parse_args(argv)
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    import runtime as rt
    state = Path(os.environ.get("AAF_SERVER_STATE") or rt.MBOX_HOME / "server")
    dbp = state / "chat.sqlite3"
    if not dbp.exists():
        print(f"找不到 AA Forum 資料庫：{dbp}", file=sys.stderr)
        return 1
    out = Path(os.environ.get("AAF_TRANSCRIPT_DIR") or rt.INST / "outputs" / "transcripts")
    with contextlib.closing(sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        r = export(db, out, rt.setting("AAF_TZ"), rooms=a.room, since=a.since)
    print(f"匯出 {r['rooms']} 個群，更新 {len(r['written'])} 個檔 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
