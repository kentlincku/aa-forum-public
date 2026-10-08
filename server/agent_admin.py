"""AA Forum「加入 agent」後端（S8）：只有 Owner 能用。

- GET  /api/agents/catalog               內建＋自訂 agent、是否已安裝、安裝指令、需要的登入；現有 skill 清單；現有角色
- POST /api/agents/plan-custom {npm}      查 npm 套件，回安裝計畫（不安裝）
- POST /api/agents/install {agent|plan}   使用者確認後才呼叫；背景安裝，回 job
- GET  /api/agents/install/{job}?offset=  安裝輸出（輪詢）
- POST /api/agents/probe {agent}          連線檢查（initialize＋session/new，不花 token）
- POST /api/agents/roles                  新增角色：寫 roles.json（dispatcher 熱重載自動發 token、入名冊、入 AA Forum）
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import runtime as rt

if str(rt.ROOT) not in sys.path:
    sys.path.insert(0, str(rt.ROOT))
from drivers import acp_catalog  # noqa: E402

router = APIRouter()
ROLE_RE = r"^[a-z][a-z0-9_-]{1,31}$"
_identity = None   # app.py 注入


def _owner(request: Request):
    if _identity(request) != "Owner":
        raise HTTPException(403, "只有擁有者可以管理 agent")


def _skills() -> list[dict]:
    out = []
    for d in sorted((rt.INST / "skills").iterdir()):
        f = d / "SKILL.md"
        if f.exists():
            head = f.read_text(encoding="utf-8", errors="replace")[:2000]
            m = re.search(r"^description:[ \t]*(.*)$((?:\n[ \t]+.*)*)", head, re.M)
            desc = ""
            if m:
                first = m[1].strip()
                desc = " ".join(x.strip() for x in m[2].splitlines() if x.strip()) if first in ("|", ">", "|-", ">-", "") else first
                desc = desc.strip("'\"")
            out.append(dict(id=d.name, path=f"skills/{d.name}/SKILL.md", description=desc[:300]))
    return out


@router.get("/api/agents/catalog")
def catalog(request: Request):
    _owner(request)
    roles = rt.load_roles()
    return dict(agents=acp_catalog.status(), skills=_skills(),
                roles=[dict(id=r, agent=c.get("acp_agent") or c.get("driver"), rank=c.get("rank"))
                       for r, c in roles.items()])


class PlanCustom(BaseModel):
    npm: str = Field(min_length=1, max_length=214)


@router.post("/api/agents/plan-custom")
def plan_custom(data: PlanCustom, request: Request):
    _owner(request)
    try:
        return acp_catalog.plan_custom(data.npm)
    except ValueError as e:
        raise HTTPException(400, str(e))


class InstallReq(BaseModel):
    agent: str = ""
    plan: dict | None = None
    confirm: bool = False


@router.post("/api/agents/install")
def install(data: InstallReq, request: Request):
    _owner(request)
    if not data.confirm:
        raise HTTPException(400, "需要確認後才安裝")
    try:
        if data.plan:
            # 重新查一次，不信任前端傳回的計畫內容
            plan = acp_catalog.plan_custom(data.plan.get("npm", ""))
            return acp_catalog.start_install(plan["id"], custom=plan)
        return acp_catalog.start_install(data.agent)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/agents/install/{job}")
def install_status(job: str, request: Request, offset: int = 0):
    _owner(request)
    try:
        return acp_catalog.job_status(job, max(0, offset))
    except KeyError:
        raise HTTPException(404, "找不到安裝工作")


class ProbeReq(BaseModel):
    agent: str


@router.post("/api/agents/probe")
def probe(data: ProbeReq, request: Request):
    _owner(request)
    if data.agent not in acp_catalog.all_agents():
        raise HTTPException(400, "不認得的 agent")
    return acp_catalog.probe(data.agent)


class RoleReq(BaseModel):
    role: str = Field(pattern=ROLE_RE)
    agent: str
    label: str = Field(default="", max_length=40)
    rank: str = Field(default="worker", pattern="^(lead|worker)$")
    skill: str = ""                       # 既有 skill id
    persona: str = Field(default="", max_length=20000)   # 或直接填人設 → 存成新 skill
    model: str = Field(default="", max_length=100)


@router.post("/api/agents/roles")
def add_role(data: RoleReq, request: Request):
    _owner(request)
    roles_file = rt.ROLES_FILE
    doc = json.loads(roles_file.read_text())
    if data.role in doc["roles"]:
        raise HTTPException(409, f"角色 {data.role} 已存在")
    if data.agent not in acp_catalog.all_agents():
        raise HTTPException(400, "不認得的 agent")
    if not acp_catalog.resolve_cmd(data.agent):
        raise HTTPException(400, f"{data.agent} 尚未安裝 ACP，請先安裝")
    if data.model and not re.fullmatch(r"[A-Za-z0-9._:/\[\]-]{1,100}", data.model):
        raise HTTPException(400, "模型名稱不合法")
    if data.persona.strip():
        sdir = rt.INST / "skills" / f"{data.role}"
        if sdir.exists():
            raise HTTPException(409, f"skills/{data.role} 已存在，請改選既有 skill")
        sdir.mkdir(parents=True)
        desc = (data.label or data.role).replace("\n", " ")
        (sdir / "SKILL.md").write_text(
            f"---\nname: {data.role}\ndescription: {desc}（AA Forum 新增的角色）\n---\n\n{data.persona.strip()}\n",
            encoding="utf-8")
        skill_path = f"skills/{data.role}/SKILL.md"
    elif data.skill:
        skill_path = f"skills/{data.skill}/SKILL.md"
        if not re.fullmatch(r"[a-z0-9_-]+", data.skill) or not (rt.INST / skill_path).exists():
            raise HTTPException(400, "skill 不存在")
    else:
        raise HTTPException(400, "請選擇 skill 或填寫人設")
    cfg = dict(runtime=f"acp-{data.agent}", rank=data.rank, persona_file=skill_path,
               driver="acp", acp_agent=data.agent)
    if data.label:
        cfg["label"] = data.label
    if data.model:
        cfg["model"] = data.model
    doc["roles"][data.role] = cfg
    fd, tmp = tempfile.mkstemp(dir=roles_file.parent, prefix=".roles.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, roles_file)      # 原子替換；dispatcher 熱重載（約 10 秒）會發 token、入名冊、入 AA Forum
    return dict(ok=True, role=data.role, config=cfg,
                message=f"已寫入 roles.json；約 10 秒內 dispatcher 會建立 {data.role} 的 token 並加入 AA Forum。")


@router.get("/api/agents/models/{agent}")
def models(agent: str, request: Request):
    _owner(request)
    if agent not in acp_catalog.all_agents():
        raise HTTPException(404, "不認得的 agent")
    if not acp_catalog.resolve_cmd(agent):
        return dict(default=None, models=[], source="尚未安裝")
    return acp_catalog.agent_models(agent)


class RoleEngine(BaseModel):
    agent: str
    model: str = Field(default="", max_length=100)


@router.post("/api/agents/roles/{role}/engine")
def set_engine(role: str, data: RoleEngine, request: Request):
    """改角色的 agent／模型（寫 roles.json；換 agent 會開新 session，換模型下一輪生效）。"""
    _owner(request)
    doc = json.loads(rt.ROLES_FILE.read_text())
    cfg = doc["roles"].get(role)
    if not cfg or cfg.get("driver") == "manual":
        raise HTTPException(404, "角色不存在或為手動角色")
    if data.agent not in acp_catalog.all_agents() or not acp_catalog.resolve_cmd(data.agent):
        raise HTTPException(400, f"{data.agent} 尚未安裝 ACP")
    if data.model and not re.fullmatch(r"[A-Za-z0-9._:/\[\]-]{1,100}", data.model):
        raise HTTPException(400, "模型名稱不合法")
    if data.model:
        chk = acp_catalog.check_model(data.agent, data.model)
        if not chk.get("ok"):
            raise HTTPException(400, f"{role}：模型 {data.model} 實測失敗，未切換：{chk.get('error', '')[:300]}")
    before = (cfg.get("acp_agent"), cfg.get("model"))
    cfg.update(driver="acp", acp_agent=data.agent, runtime=f"acp-{data.agent}")
    cfg.pop("adapter", None)
    if data.model:
        cfg["model"] = data.model
    else:
        cfg.pop("model", None)
    if before == (cfg.get("acp_agent"), cfg.get("model")):
        return dict(ok=True, changed=False)
    fd, tmp = tempfile.mkstemp(dir=rt.ROLES_FILE.parent, prefix=".roles.", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, rt.ROLES_FILE)
    # AA Forum 的臨時 override 會蓋過 roles.json：改了就清掉，避免看起來沒生效
    try:
        sys.path.insert(0, str(rt.ROOT))
        from mbox.overrides import override_path
        override_path(role, rt.MBOX_HOME).unlink(missing_ok=True)
    except Exception:
        pass
    return dict(ok=True, changed=True)
