"""P1：核心不寫死使用者名稱；human 一律由 roles.json rank=human 決定（可多人）。"""
import io, json, re, tokenize, ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import base64
PERSON = re.compile(r"(?<![\w-])" + base64.b64decode("a2VudA==").decode() + r"(?![\w-])", re.I)


def _code_tokens(path):
    src = path.read_text(encoding="utf-8")
    docs = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant):
                docs.add(first.lineno)
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT or (tok.type == tokenize.STRING and tok.start[0] in docs):
            continue
        yield tok.start[0], tok.string


def test_core_code_has_no_person_name():
    hits = []
    for f in [*ROOT.glob("mbox/*.py"), *ROOT.glob("server/*.py"), *ROOT.glob("drivers/*.py")]:
        for n, s in _code_tokens(f):
            if PERSON.search(s):
                hits.append(f"{f.relative_to(ROOT)}:{n}: {s[:80]}")
    for f in [*ROOT.glob("server/*.js"), *ROOT.glob("server/*.html")]:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = re.sub(r"//.*$|/\*.*?\*/|<!--.*?-->", "", line)
            if PERSON.search(code):
                hits.append(f"{f.relative_to(ROOT)}:{n}")
    for name in ("aaf", "mbox", "aaf-chat"):
        p = ROOT / "bin" / name
        if p.exists():
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0] if not line.lstrip().startswith("#!") else ""
                if PERSON.search(code):
                    hits.append(f"bin/{name}:{n}")
    assert not hits, "核心寫死了使用者名稱：\n" + "\n".join(hits)


def test_any_human_can_receive_from_room_context(tmp_path):
    from mbox.core import Store
    s = Store(tmp_path / "m.sqlite3")
    for a, rank in (("alice", "human"), ("bob", "human"), ("w1", "worker"), ("w2", "worker")):
        s.add_agent(a, "test", rank)
    s.set_room_members(5, ["w1"])
    me = {"id": "w1", "rank": "worker", "runtime": "test"}
    for h in ("alice", "bob"):
        s.send(me, h, "hi", source_room=5)          # 兩位 human 都不受群範圍限制
    import pytest
    from mbox.core import MboxError
    with pytest.raises(MboxError):
        s.send(me, "w2", "hi", source_room=5)       # 群外的非人類角色仍擋


def test_runtime_human_ids_from_roles(tmp_path, monkeypatch):
    import server.runtime as rt
    f = tmp_path / "roles.json"
    f.write_text(json.dumps({"roles": {"w": {"rank": "worker"}, "alice": {"rank": "human"}, "bob": {"rank": "human"}}}))
    monkeypatch.setattr(rt, "ROLES_FILE", f)
    assert rt.human_ids() == ["alice", "bob"]
    assert rt.human_id() == "alice"
    f.write_text(json.dumps({"roles": {"w": {"rank": "worker"}}}))
    assert rt.human_id() == rt.HUMAN_FALLBACK
