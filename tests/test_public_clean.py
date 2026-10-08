"""公版：程式碼（含註解）不得出現特定使用者、主機或產品字樣。"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import base64
# 私有字樣（base64 存放，避免本檔自己出現這些字）
_WORDS = base64.b64decode("a2VudHxsaW5oc3VhbnBvfHNwYXJrfHN5bmVyZ3l8YTFjfHJkZDE=").decode()
# 開源前另加：舊專案名與內部代號、私人帳號、家目錄路徑
_OLD = base64.b64decode("Y2l2aWxpYW58emhvbmdrb25nfOeqqeirlnzkuK3mjqflrqR8a2VudGxpbmNrdQ==").decode()
BAD = re.compile(rf"(?<![\w-])({_WORDS})(?![\w-])|192\.168\.|/Users/(?!u/|u-)|/home/(?!user/|alice/|tester/)\w|{_OLD}", re.I)
SELF = {"tests/test_public_clean.py", "tests/test_pc1_no_person_names.py"}


def test_no_private_names_in_tracked_files():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    if not files:   # 尚未 commit（初始化時）：掃工作目錄
        files = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file()
                 and not any(x in p.parts for x in (".git", ".venv", "__pycache__", "node_modules", "var", "work"))]
    hits = []
    for f in files:
        if f in SELF or f.startswith(("web/node_modules/", "vendor/acp/node_modules/")):
            continue
        p = ROOT / f
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if BAD.search(line):
                hits.append(f"{f}:{n}: {line.strip()[:100]}")
    assert not hits, "公版出現私有字樣：\n" + "\n".join(hits)
