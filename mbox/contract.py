"""契約（contract/v1）的載入與驗證。核心與 driver 之間只交換符合這些 schema 的 JSON。"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_DIR = ROOT / "contract"
VERSION = "1"
KINDS = ("wake_request", "turn_result", "manifest", "usage")


class ContractError(ValueError):
    pass


@lru_cache(maxsize=None)
def schema(kind: str, version: str = VERSION) -> dict:
    if kind not in KINDS:
        raise ContractError(f"未知的契約種類：{kind}")
    return json.loads((CONTRACT_DIR / f"v{version}" / f"{kind}.schema.json").read_text(encoding="utf-8"))


def validate(kind: str, obj: dict) -> dict:
    """驗證通過回傳 obj；否則丟 ContractError（訊息含欄位路徑）。"""
    import jsonschema
    v = jsonschema.Draft202012Validator(schema(kind, str(obj.get("contract_version", VERSION))))
    errs = sorted(v.iter_errors(obj), key=lambda e: list(e.path))
    if errs:
        raise ContractError("; ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errs[:5]))
    return obj


def empty_tokens() -> dict:
    return {"input": None, "output": None, "cache_read": None, "cache_write": None, "reasoning": None}


def turn_result(role: str, driver: str, exit_code: int | None, **kw) -> dict:
    """組一份 TurnResult；未提供的選填欄位不出現或為 None。ok 預設＝exit==0 且無 error。"""
    tr = {"contract_version": VERSION, "role": role, "driver": driver, "exit": exit_code,
          "ok": kw.pop("ok", exit_code == 0 and not kw.get("error"))}
    tok = empty_tokens()
    tok.update({k: v for k, v in (kw.pop("tokens", None) or {}).items() if k in tok})
    tr["tokens"] = tok
    for k in ("session_id", "text", "error", "model_used", "duration_ms", "started_at", "finished_at"):
        if k in kw:
            tr[k] = kw.pop(k)
    if kw:
        raise ContractError(f"TurnResult 不認得的欄位：{sorted(kw)}")
    return tr
