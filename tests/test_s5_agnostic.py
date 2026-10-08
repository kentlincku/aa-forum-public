"""S5：核心不認識 agent —— mbox/、server/、bin/aaf 的程式碼裡不得出現任何 agent 名稱。

只允許出現在註解與 docstring（說明、範例）。agent 專屬知識一律放 drivers/。
"""
import ast
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAMES = re.compile(r"(?<![\w.-])(hermes|codex|claude|gemini|pi)(?![\w-])", re.I)


def py_code_strings(path):
    """回傳程式碼 token（去掉註解與 docstring）。"""
    src = path.read_text(encoding="utf-8")
    docs = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                    and isinstance(first.value.value, str):
                docs.add(first.lineno)
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT or (tok.type == tokenize.STRING and tok.start[0] in docs):
            continue
        out.append((tok.start[0], tok.string))
    return out


def web_code_lines(path):
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        code = re.sub(r"//.*$|/\*.*?\*/|<!--.*?-->", "", line)
        out.append((n, code))
    return out


def test_core_has_no_agent_names():
    files = [*ROOT.glob("mbox/*.py"), *ROOT.glob("server/*.py")]
    hits = []
    for f in files:
        for n, s in py_code_strings(f):
            if NAMES.search(s):
                hits.append(f"{f.relative_to(ROOT)}:{n}: {s[:80]}")
    for f in [*ROOT.glob("server/*.js"), *ROOT.glob("server/*.html")]:
        for n, s in web_code_lines(f):
            if NAMES.search(s):
                hits.append(f"{f.relative_to(ROOT)}:{n}: {NAMES.search(s).group(0)}")
    for n, line in enumerate((ROOT / "bin" / "aaf").read_text(encoding="utf-8").splitlines(), 1):
        code = line.split("#", 1)[0] if not line.lstrip().startswith("#!") else ""
        if NAMES.search(code):
            hits.append(f"bin/aaf:{n}: {code.strip()[:80]}")
    assert not hits, "核心出現 agent 名稱（請移入 drivers/）：\n" + "\n".join(hits)
