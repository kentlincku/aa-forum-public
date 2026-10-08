"""控制台「Skills」後端（SPEC-1.1 §3）：只有使用者（rank=human／Owner）能改；agent 只能讀。

GET    /api/skills                         清單：skill、分類包、群指派、角色指派、使用中
GET    /api/skills/{id}                    內容
POST   /api/skills                         新增 {id, content}
PUT    /api/skills/{id}                    編輯 {content, note}
DELETE /api/skills/{id}                    刪除（使用中會被擋，回 409 並列出使用者）
GET    /api/skills/{id}/history            版本與操作紀錄
GET    /api/skills/{id}/history/{ver}      某版本內容
POST   /api/skills/{id}/restore {version}  還原
PUT    /api/skill-packs/{name} {skills}    建立／修改分類包
DELETE /api/skill-packs/{name}             刪除包（使用中會被擋）
PUT    /api/skill-assign/role/{role}       {skills, packs}
PUT    /api/skill-assign/room/{room}       {skills, packs}
GET    /api/skill-preview?role=&room=      實際會載入哪些 skill（依序）
"""
from __future__ import annotations

import sys

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import runtime as rt

if str(rt.ROOT) not in sys.path:
    sys.path.insert(0, str(rt.ROOT))
from mbox import skills as sk  # noqa: E402

router = APIRouter()
_identity = None   # app.py 注入
_is_human = None   # app.py 注入


def _who(request: Request) -> str:
    return _identity(request)


def _human(request: Request) -> str:
    who = _who(request)
    if not _is_human(who):
        raise HTTPException(403, "skill 只能由使用者修改；agent 要改請發提案訊息")
    return who


def _wrap(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except sk.SkillError as e:
        raise HTTPException(e.status, str(e))


class NewSkill(BaseModel):
    id: str = Field(max_length=64)
    content: str = Field(max_length=sk.MAX_SKILL_BYTES)


class EditSkill(BaseModel):
    content: str = Field(max_length=sk.MAX_SKILL_BYTES)
    note: str = Field(default="", max_length=300)


class Restore(BaseModel):
    version: str = Field(max_length=40)


class Pack(BaseModel):
    skills: list[str] = Field(default_factory=list, max_length=200)


class Assign(BaseModel):
    skills: list[str] = Field(default_factory=list, max_length=200)
    packs: list[str] = Field(default_factory=list, max_length=50)


@router.get("/api/skills")
def list_skills(request: Request):
    _who(request)
    return sk.listing()


@router.get("/api/skills/{sid}")
def get_skill(sid: str, request: Request):
    _who(request)
    return dict(id=sid, content=_wrap(sk.read, sid), used_by=sk.usage().get(sid, []))


@router.post("/api/skills")
def create_skill(data: NewSkill, request: Request):
    return _wrap(sk.save, data.id, data.content, _human(request), create=True)


@router.put("/api/skills/{sid}")
def edit_skill(sid: str, data: EditSkill, request: Request):
    return _wrap(sk.save, sid, data.content, _human(request), note=data.note)


@router.delete("/api/skills/{sid}")
def delete_skill(sid: str, request: Request):
    return _wrap(sk.delete, sid, _human(request))


@router.get("/api/skills/{sid}/history")
def skill_history(sid: str, request: Request):
    _who(request)
    versions, log = _wrap(sk.history, sid)
    return dict(versions=versions, log=log)


@router.get("/api/skills/{sid}/history/{ver}")
def skill_version(sid: str, ver: str, request: Request):
    _who(request)
    return dict(id=sid, version=ver, content=_wrap(sk.read_version, sid, ver))


@router.post("/api/skills/{sid}/restore")
def restore_skill(sid: str, data: Restore, request: Request):
    return _wrap(sk.restore, sid, data.version, _human(request))


@router.put("/api/skill-packs/{name}")
def put_pack(name: str, data: Pack, request: Request):
    return _wrap(sk.set_pack, name, data.skills, _human(request))


@router.delete("/api/skill-packs/{name}")
def del_pack(name: str, request: Request):
    return _wrap(sk.delete_pack, name, _human(request))


@router.put("/api/skill-assign/role/{role}")
def assign_role(role: str, data: Assign, request: Request):
    return _wrap(sk.assign_role, role, data.skills, data.packs, _human(request))


@router.put("/api/skill-assign/room/{room}")
def assign_room(room: int, data: Assign, request: Request):
    return _wrap(sk.assign_room, room, data.skills, data.packs, _human(request))


@router.get("/api/skill-preview")
def preview(request: Request, role: str, room: int | None = None):
    _who(request)
    cfg = sk.listing()["roles"].get(role)
    if cfg is not None:
        cfg = {**cfg, "persona_file": cfg.get("persona_file")}
    if cfg is None:
        raise HTTPException(404, f"沒有角色 {role}")
    persona = cfg.get("persona_file")
    return dict(role=role, room=room, persona=persona, skills=[sid for sid, _ in sk.compose(cfg, room)])
