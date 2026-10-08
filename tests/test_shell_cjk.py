"""macOS bash 3.2：`$var` 後面緊接全形字會被吃進變數名（unbound variable）。所有 shell 腳本必須寫成 ${var}。"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BAD = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*(?=[^\x00-\x7f])")


def test_no_unbraced_var_before_cjk():
    files = [p for p in (ROOT / "bin").iterdir() if p.is_file()]
    files += list((ROOT / "tools").glob("*.sh")) + list((ROOT / "examples").rglob("*.sh"))
    hits = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if not text.startswith("#!") or "python" in text.splitlines()[0]:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            code = line.split(" #", 1)[0] if not line.lstrip().startswith("#") else ""
            if BAD.search(code):
                hits.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()[:90]}")
    assert not hits, "請改成 ${var}：\n" + "\n".join(hits)
