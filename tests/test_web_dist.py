"""The built web UI ships in git (users run it without Node). Guard against a missing or stale build."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "web" / "dist"


def test_dist_is_committed_and_complete():
    tracked = subprocess.run(["git", "ls-files", "web/dist"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    if not tracked:            # not a git checkout (e.g. a tarball): just check the files exist
        tracked = [str(p.relative_to(ROOT)) for p in DIST.rglob("*") if p.is_file()]
    assert "web/dist/index.html" in tracked
    html = (DIST / "index.html").read_text()
    for ref in re.findall(r'(?:src|href)="/app/(assets/[^"]+)"', html):
        assert f"web/dist/{ref}" in tracked, f"index.html refers to {ref} which is not committed"


def test_dist_not_older_than_sources():
    """If sources changed after the build, rebuild: cd web && npm run build."""
    built = max(p.stat().st_mtime for p in DIST.rglob("*") if p.is_file())
    src = [p for p in (ROOT / "web" / "src").rglob("*") if p.is_file() and ".test." not in p.name]
    newest = max(src, key=lambda p: p.stat().st_mtime)
    assert newest.stat().st_mtime <= built + 1, f"web/dist is older than {newest.relative_to(ROOT)}; run npm run build"
