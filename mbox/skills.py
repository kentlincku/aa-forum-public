"""Skill 管理（SPEC-1.1 §3）：實例 skills/ 目錄、分類包、指派、版本紀錄、組合載入。

檔案配置（都在實例根 AAF_HOME 之下）：
  skills/<id>/SKILL.md              skill 本體（id：小寫英數、-、_）
  skills/_config.json               {"packs": {包名: [skill id...]}, "rooms": {"<群號>": {"skills": [...], "packs": [...]}}}
  deploy/roles.json                 角色指派：roles.<r>.skills、roles.<r>.skill_packs（persona_file 照舊是人設）
  var/skill_history/<id>/<時間>.md  每次儲存／刪除前的版本
  var/skill_history.jsonl           操作紀錄（誰、何時、動作、skill、摘要）

載入順序（compose）：人設 persona_file → 角色 skills／包 → 群 skills／包；重複只載一次。
寫入一律原子替換；只給 rank=human 用（API 層檢查）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from mbox import paths as _paths

SKILL_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PACK_ID = re.compile(r"^[\w\u4e00-\u9fff-]{1,40}$")
MAX_SKILL_BYTES = 200_000


class SkillError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _inst() -> Path:
    return _paths.instance()


def skills_dir() -> Path:
    return _inst() / "skills"


def _config_file() -> Path:
    return skills_dir() / "_config.json"


def _roles_file() -> Path:
    return Path(os.environ.get("MBOX_ROLES") or _inst() / "deploy" / "roles.json")


def _history_dir() -> Path:
    return Path(os.environ.get("MBOX_HOME") or _inst() / "var") / "skill_history"


def _atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _check_id(sid: str):
    if not SKILL_ID.match(sid or ""):
        raise SkillError(400, "skill 名稱只能用小寫英數、-、_（1–64 字，英數開頭）")


# ---------------- 設定檔 ----------------
def load_config() -> dict:
    f = _config_file()
    try:
        c = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except (OSError, ValueError):
        c = {}
    c.setdefault("packs", {})
    c.setdefault("rooms", {})
    return c


def save_config(c: dict):
    _atomic_write(_config_file(), json.dumps(c, ensure_ascii=False, indent=1) + "\n")


def _load_roles_doc() -> dict:
    return json.loads(_roles_file().read_text(encoding="utf-8"))


def _save_roles_doc(doc: dict):
    _atomic_write(_roles_file(), json.dumps(doc, ensure_ascii=False, indent=1) + "\n")


# ---------------- 讀取 ----------------
def _description(text: str) -> str:
    head = text[:2000]
    m = re.search(r"^description:[ \t]*(.*)$((?:\n[ \t]+.*)*)", head, re.M)
    if not m:
        return ""
    first = m[1].strip()
    desc = " ".join(x.strip() for x in m[2].splitlines() if x.strip()) if first in ("|", ">", "|-", ">-", "") else first
    return desc.strip("'\"")[:300]


def skill_ids() -> list[str]:
    d = skills_dir()
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and (p / "SKILL.md").exists() and SKILL_ID.match(p.name))


def read(sid: str) -> str:
    _check_id(sid)
    f = skills_dir() / sid / "SKILL.md"
    if not f.exists():
        raise SkillError(404, f"skill {sid} 不存在")
    return f.read_text(encoding="utf-8", errors="replace")


def usage() -> dict[str, list[str]]:
    """skill id → 使用者清單（「角色 lead（人設）」「角色 builder」「群 3」「包 build」）。"""
    out: dict[str, list[str]] = {}
    add = lambda sid, who: out.setdefault(sid, []).append(who)
    cfg = load_config()
    try:
        roles = _load_roles_doc().get("roles", {})
    except (OSError, ValueError):
        roles = {}
    for r, c in roles.items():
        pf = c.get("persona_file") or ""
        m = re.match(r"^skills/([^/]+)/SKILL\.md$", pf)
        if m:
            add(m[1], f"角色 {r}（人設）")
        for s in c.get("skills") or []:
            add(s, f"角色 {r}")
        for p in c.get("skill_packs") or []:
            for s in cfg["packs"].get(p, []):
                add(s, f"角色 {r}（經由包 {p}）")
    for room, a in cfg["rooms"].items():
        for s in a.get("skills") or []:
            add(s, f"群 {room}")
        for p in a.get("packs") or []:
            for s in cfg["packs"].get(p, []):
                add(s, f"群 {room}（經由包 {p}）")
    for p, members in cfg["packs"].items():
        for s in members:
            add(s, f"包 {p}")
    return out


def listing() -> dict:
    cfg = load_config()
    used = usage()
    packs_of: dict[str, list[str]] = {}
    for p, members in cfg["packs"].items():
        for s in members:
            packs_of.setdefault(s, []).append(p)
    items = []
    for sid in skill_ids():
        f = skills_dir() / sid / "SKILL.md"
        text = f.read_text(encoding="utf-8", errors="replace")
        items.append(dict(id=sid, description=_description(text), packs=sorted(packs_of.get(sid, [])),
                          used_by=used.get(sid, []), bytes=len(text.encode()), mtime=f.stat().st_mtime))
    try:
        roles = _load_roles_doc().get("roles", {})
    except (OSError, ValueError):
        roles = {}
    return dict(skills=items, packs=cfg["packs"], rooms=cfg["rooms"],
                roles={r: dict(persona_file=c.get("persona_file"), skills=c.get("skills") or [],
                               skill_packs=c.get("skill_packs") or [], rank=c.get("rank"))
                       for r, c in roles.items()})


# ---------------- 版本紀錄 ----------------
def _log(actor: str, action: str, sid: str, note: str = ""):
    h = _history_dir()
    h.mkdir(parents=True, exist_ok=True)
    with open(h.parent / "skill_history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(dict(at=time.time(), actor=actor, action=action, skill=sid, note=note[:300]),
                           ensure_ascii=False) + "\n")


def _snapshot(sid: str) -> str | None:
    """把目前版本存進歷史；回傳版本名。"""
    f = skills_dir() / sid / "SKILL.md"
    if not f.exists():
        return None
    d = _history_dir() / sid
    d.mkdir(parents=True, exist_ok=True)
    base = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    ver, n = base, 1
    while (d / f"{ver}.md").exists():     # 同一毫秒連續兩次（例：還原時先存目前版）不能互相覆蓋
        ver, n = f"{base}-{n}", n + 1
    shutil.copy2(f, d / f"{ver}.md")
    return ver


def history(sid: str) -> tuple[list[dict], list[dict]]:
    _check_id(sid)
    d = _history_dir() / sid
    vers = sorted((p for p in d.glob("*.md")), reverse=True) if d.is_dir() else []
    log = []
    lf = _history_dir().parent / "skill_history.jsonl"
    if lf.exists():
        for line in lf.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("skill") == sid:
                log.append(e)
    return [dict(version=p.stem, bytes=p.stat().st_size) for p in vers], log[-100:]


def read_version(sid: str, ver: str) -> str:
    _check_id(sid)
    if not re.fullmatch(r"\d{8}-\d{6}-\d{3}(-\d{1,3})?", ver):
        raise SkillError(400, "版本名稱不合法")
    f = _history_dir() / sid / f"{ver}.md"
    if not f.exists():
        raise SkillError(404, "沒有這個版本")
    return f.read_text(encoding="utf-8", errors="replace")


# ---------------- 寫入 ----------------
def save(sid: str, text: str, actor: str, create: bool = False, note: str = "") -> dict:
    _check_id(sid)
    if len(text.encode()) > MAX_SKILL_BYTES:
        raise SkillError(413, f"skill 超過 {MAX_SKILL_BYTES // 1000} KB")
    if not text.strip():
        raise SkillError(422, "內容不能是空的")
    f = skills_dir() / sid / "SKILL.md"
    if create and f.exists():
        raise SkillError(409, f"skill {sid} 已存在")
    if not create and not f.exists():
        raise SkillError(404, f"skill {sid} 不存在")
    if f.exists() and f.read_text(encoding="utf-8", errors="replace") == text:
        return dict(ok=True, changed=False)
    ver = _snapshot(sid)
    _atomic_write(f, text)
    _log(actor, "create" if create else "edit", sid, note or (f"前一版 {ver}" if ver else ""))
    return dict(ok=True, changed=True, previous=ver)


def restore(sid: str, ver: str, actor: str) -> dict:
    text = read_version(sid, ver)
    r = save(sid, text, actor, note=f"還原到 {ver}")
    _log(actor, "restore", sid, ver)
    return r


def delete(sid: str, actor: str) -> dict:
    _check_id(sid)
    d = skills_dir() / sid
    if not (d / "SKILL.md").exists():
        raise SkillError(404, f"skill {sid} 不存在")
    used = usage().get(sid)
    if used:
        raise SkillError(409, "這個 skill 正在使用中，先取消指派再刪：" + "、".join(used))
    ver = _snapshot(sid)
    shutil.rmtree(d)
    _log(actor, "delete", sid, f"刪除前版本 {ver}")
    return dict(ok=True, previous=ver)


def set_pack(name: str, members: list[str], actor: str) -> dict:
    if not PACK_ID.match(name or ""):
        raise SkillError(400, "包名只能用中英文、數字、-、_（1–40 字）")
    known = set(skill_ids())
    bad = [s for s in members if s not in known]
    if bad:
        raise SkillError(400, "沒有這些 skill：" + "、".join(bad))
    cfg = load_config()
    cfg["packs"][name] = sorted(dict.fromkeys(members))
    save_config(cfg)
    _log(actor, "pack", name, ",".join(members))
    return dict(ok=True, pack=name, skills=cfg["packs"][name])


def delete_pack(name: str, actor: str) -> dict:
    cfg = load_config()
    if name not in cfg["packs"]:
        raise SkillError(404, "沒有這個包")
    users = [f"群 {r}" for r, a in cfg["rooms"].items() if name in (a.get("packs") or [])]
    try:
        users += [f"角色 {r}" for r, c in _load_roles_doc().get("roles", {}).items() if name in (c.get("skill_packs") or [])]
    except (OSError, ValueError):
        pass
    if users:
        raise SkillError(409, "這個包正在使用中，先取消指派再刪：" + "、".join(users))
    del cfg["packs"][name]
    save_config(cfg)
    _log(actor, "pack-delete", name)
    return dict(ok=True)


def _validate_assign(skills: list[str], packs: list[str]):
    known, cfg = set(skill_ids()), load_config()
    bad = [s for s in skills if s not in known] + [f"包 {p}" for p in packs if p not in cfg["packs"]]
    if bad:
        raise SkillError(400, "不存在：" + "、".join(bad))


def assign_role(role: str, skills: list[str], packs: list[str], actor: str) -> dict:
    _validate_assign(skills, packs)
    doc = _load_roles_doc()
    if role not in doc.get("roles", {}):
        raise SkillError(404, f"沒有角色 {role}")
    c = doc["roles"][role]
    for k, v in (("skills", skills), ("skill_packs", packs)):
        if v:
            c[k] = list(dict.fromkeys(v))
        else:
            c.pop(k, None)
    _save_roles_doc(doc)      # dispatcher 熱重載；下次叫醒套用
    _log(actor, "assign-role", role, f"skills={skills} packs={packs}")
    return dict(ok=True, role=role, skills=c.get("skills", []), skill_packs=c.get("skill_packs", []))


def assign_room(room: int, skills: list[str], packs: list[str], actor: str) -> dict:
    _validate_assign(skills, packs)
    cfg = load_config()
    key = str(int(room))
    if skills or packs:
        cfg["rooms"][key] = dict(skills=list(dict.fromkeys(skills)), packs=list(dict.fromkeys(packs)))
    else:
        cfg["rooms"].pop(key, None)
    save_config(cfg)
    _log(actor, "assign-room", key, f"skills={skills} packs={packs}")
    return dict(ok=True, room=int(room), **cfg["rooms"].get(key, dict(skills=[], packs=[])))


# ---------------- 組合 ----------------
def compose(cfg: dict, room: int | None = None) -> list[tuple[str, Path]]:
    """角色（＋群）實際要載入的 skill：[(id, 路徑)]，依序、去重。人設 persona_file 不在這裡（system_prompt 另外處理）。"""
    conf = load_config()
    order: list[str] = []
    order += list(cfg.get("skills") or [])
    for p in cfg.get("skill_packs") or []:
        order += conf["packs"].get(p, [])
    if room is not None:
        a = conf["rooms"].get(str(room), {})
        order += list(a.get("skills") or [])
        for p in a.get("packs") or []:
            order += conf["packs"].get(p, [])
    persona = cfg.get("persona_file") or ""
    m = re.match(r"^skills/([^/]+)/SKILL\.md$", persona)
    seen = {m[1]} if m else set()
    out = []
    for sid in order:
        if sid in seen or not SKILL_ID.match(sid):
            continue
        seen.add(sid)
        f = skills_dir() / sid / "SKILL.md"
        if f.exists():
            out.append((sid, f))
    return out
