#!/usr/bin/env bash
# Linux 實機驗證（SPEC-1.0 §4，S8＝本機 Docker 容器）。可重跑。
#   tools/verify-linux.sh [image...]     預設：debian:bookworm ubuntu:24.04（本機架構）＋ debian:bookworm（linux/amd64）
# 每個映像：裝前置條件 → bin/aaf install → 完整 pytest → init → up → echo 往返 → AA Forum 登入／發文／上傳
#           → 逐字稿 → supervisor 重開 → doctor → down。結果寫 docs/verify/<日期>-linux.md。
# 真的 agent（Hermes／Claude／Codex）需要登入，容器內不跑（UNVERIFIED），只驗 command driver。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
DAY=$(date +%Y%m%d)
REPORT="$ROOT/docs/verify/${DAY}-linux.md"
mkdir -p "$ROOT/docs/verify"
command -v docker >/dev/null || { echo "需要 docker（macOS：colima start）" >&2; exit 1; }

TARGETS=("$@")
[ ${#TARGETS[@]} -gt 0 ] || TARGETS=("debian:bookworm" "ubuntu:24.04" "linux/amd64=debian:bookworm")

# 容器內執行的腳本：輸出「RESULT <項目> PASS|FAIL <說明>」
read -r -d '' INNER <<'SH'
set -u
export DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
r() { echo "RESULT $1 $2 ${3:-}"; }
apt-get update -qq >/dev/null && apt-get install -y -qq python3 python3-venv python3-pip sqlite3 curl git procps ca-certificates tmux nodejs npm >/dev/null 2>&1 \
  && r prereq PASS "$(. /etc/os-release; echo "$PRETTY_NAME") $(uname -m) $(python3 -V)" || { r prereq FAIL apt; exit 1; }
useradd -m tester && mkdir /home/tester/core && (cd /src && git -c safe.directory=/src ls-files -z | xargs -0 tar cf -) | tar xf - -C /home/tester/core \
  && chown -R tester /home/tester/core || { r copy FAIL "git ls-files"; exit 1; }
su tester -s /bin/bash -w EMULATED -c '
set -u
r() { echo "RESULT $1 $2 ${3:-}"; }
cd ~/core
bin/aaf install > /tmp/install.log 2>&1 && r install PASS "$(grep -c ✓ /tmp/install.log) 項 ✓" || { r install FAIL "$(tail -3 /tmp/install.log | tr "\n" " ")"; exit 1; }
if [ "${EMULATED:-0}" = 1 ]; then
  # QEMU 使用者模式：每個程序的命令列前面多了 /usr/bin/qemu-x86_64，bin/aaf 以「argv[0] 是本 .venv 的 python」
  # 辨認自己的服務會全部認不得；且慢約 10 倍，計時類測試逾時。原生 x86_64 沒有這兩件事 ⇒ 模擬環境只跑不依賴
  # 程序辨識與計時的部分：純邏輯與 API 測試。
  out=$(.venv/bin/python -B -m pytest -q -p no:cacheprovider tests/test_mbox.py tests/test_contract.py tests/test_pc2_secrets.py \
        tests/test_pc4_hooks.py tests/test_pc5_instance.py tests/test_public_clean.py tests/test_s5_agnostic.py 2>&1 | tail -1)
  case "$out" in *failed*|*error*) r pytest-subset FAIL "$out" ;; *passed*) r pytest-subset PASS "${out}（模擬環境，見腳本註解）" ;; *) r pytest-subset FAIL "$out" ;; esac
else
  out=$(.venv/bin/python -B -m pytest -q tests -p no:cacheprovider 2>&1 | tail -1)
  case "$out" in *failed*|*error*) r pytest FAIL "$out" ;; *passed*) r pytest PASS "$out" ;; *) r pytest FAIL "$out" ;; esac
fi
bin/aaf init ~/team --user=tester > /tmp/init.log 2>&1 && r init PASS "$(head -1 /tmp/init.log)" || r init FAIL "$(cat /tmp/init.log)"
export AAF_HOME=~/team
bin/aaf up < /dev/null > /tmp/up.log 2>&1; sleep 4
bin/aaf status | grep -c "執行中" | grep -q 4 && r up PASS "4 個服務執行中" || r up FAIL "$(cat /tmp/up.log | tail -3 | tr "\n" " ")"
MBOX_AGENT=tester bin/mbox send echo "linux 往返" >/dev/null
ok=0; for i in $(seq 1 20); do sleep 2; MBOX_AGENT=tester bin/mbox inbox --all 2>/dev/null | grep -q "echo: linux 往返" && { ok=1; break; }; done
[ $ok = 1 ] && r echo-roundtrip PASS "echo 回信 $((i*2)) 秒內" || r echo-roundtrip FAIL "$(tail -5 ~/team/var/dispatcher.log | tr "\n" " ")"
ZP=$(sed -n "s/^AAF_SERVER_PORT=//p" ~/team/.aaf.env)
.venv/bin/python - "$ZP" <<PY
import json, sys, base64, urllib.request
p = sys.argv[1]; base = f"http://127.0.0.1:{p}"
def call(m, path, data=None, tok=None):
    h = {"content-type": "application/json"}
    if tok: h["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(base + path, method=m, data=json.dumps(data).encode() if data is not None else None, headers=h)
    try:
        with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e: return e.code, {}
def res(k, ok, d): print("RESULT", k, "PASS" if ok else "FAIL", d)
s, _ = call("POST", "/setup", {"username": "tester", "password": "pw-linux-1234"}); res("web-setup", s == 200, s)
s, b = call("POST", "/login", {"username": "tester", "password": "pw-linux-1234"}); tok = b.get("token"); res("web-login", s == 200 and tok, s)
s, _ = call("POST", "/login", {"username": "tester", "password": "wrong-pass-1"}); res("web-login-reject", s == 401, s)
s, room = call("POST", "/api/rooms", {"name": "linux", "members": ["echo"]}, tok); rid = room.get("id"); res("web-room", s == 200, s)
s, _ = call("POST", f"/api/rooms/{rid}/messages", {"body": "linux 群文", "client_id": "l1"}, tok); res("web-post", s == 200, s)
big = base64.b64encode(b"x" * (25 * 1024 * 1024)).decode()
s, _ = call("POST", f"/api/rooms/{rid}/messages", {"client_id": "l2", "file": {"name": "big.bin", "content": big}}, tok)
res("web-attach-25MB", s == 200, s)
s, cfg = call("GET", "/api/config", None, tok); res("web-config", s == 200 and cfg.get("version"), cfg.get("version"))
req = urllib.request.Request(base + "/files/api/upload?space=outputs&name=up.bin", method="POST", data=b"y" * (120 * 1024 * 1024),
                             headers={"Authorization": "Bearer " + tok, "x-portal-request": "1", "content-type": "application/octet-stream"})
try:
    with urllib.request.urlopen(req) as r: res("files-upload-120MB", r.status == 201, r.status)
except urllib.error.HTTPError as e: res("files-upload-120MB", False, e.code)
except Exception as e: res("files-upload-120MB", False, type(e).__name__ + ": " + str(e)[:80])
PY
bin/aaf transcript > /tmp/tr.log 2>&1
f=$(grep -l "linux 群文" ~/team/outputs/transcripts/*/20*.md 2>/dev/null | head -1)
[ -n "$f" ] && [ -f "$(dirname "$f")/_index.md" ] && r transcript PASS "$(cat /tmp/tr.log)；$(basename "$(dirname "$f")")/$(basename "$f")" || r transcript FAIL "$(cat /tmp/tr.log)"
zpid=$(cat ~/team/var/server.pid); kill -9 "$zpid"; ok=0
for i in $(seq 1 20); do sleep 1; n=$(cat ~/team/var/server.pid 2>/dev/null); [ -n "$n" ] && [ "$n" != "$zpid" ] && kill -0 "$n" 2>/dev/null && { ok=1; break; }; done
for j in $(seq 1 30); do curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${ZP}/login" | grep -q 200 && break; sleep 1; done   # 重開後等 AA Forum 就緒再做 doctor
[ $ok = 1 ] && r supervisor-restart PASS "AA Forum $zpid → $n" || r supervisor-restart FAIL "$(tail -3 ~/team/var/supervisor.log | tr "\n" " ")"
d=$(bin/aaf doctor --no-ssh 2>&1); echo "$d" | grep -q "^FAIL" && r doctor FAIL "$(echo "$d" | grep FAIL | tr "\n" " ")" || r doctor PASS "$(echo "$d" | grep -c "^OK") 項 OK"
bin/aaf secret set demo-s <<< "v1" >/dev/null 2>&1; bin/aaf secret check demo-s | grep -q "存在（file）" && r secret-file PASS "Linux 機密走 0600 檔" || r secret-file FAIL ""
bin/aaf down > /tmp/down.log 2>&1; sleep 2
left=$(ps -u tester -o pid=,args= | grep -E "^ *[0-9]+ \S*python\S* (-m mbox\.(broker|dispatcher)|server/app\.py)" )
[ -z "$left" ] && r down PASS "無殘留程序" || r down FAIL "$left"
'
SH

{
  echo "# Linux 驗證（$(date '+%F %T')）"
  echo
  echo "公版：$(git -C "$ROOT" rev-parse --short HEAD)（$(cat "$ROOT/VERSION")）；Docker：$(docker info --format '{{.OperatingSystem}} {{.Architecture}}' 2>/dev/null)"
  echo "真的 agent（Hermes／Claude／Codex）需要登入，容器內不跑：UNVERIFIED。"
  echo
} > "$REPORT"

overall=0
for t in "${TARGETS[@]}"; do
  plat=""; img="$t"
  case "$t" in *=*) plat="${t%%=*}"; img="${t#*=}" ;; esac
  label="$img${plat:+ ($plat)}"
  echo "== $label"
  emu=0; [ -n "$plat" ] && [ "${plat#linux/}" != "$(docker info --format '{{.Architecture}}' | sed 's/aarch64/arm64/;s/x86_64/amd64/')" ] && emu=1
  out=$(docker run --rm ${plat:+--platform "$plat"} -e EMULATED=$emu -v "$ROOT":/src:ro "$img" bash -c "$INNER" 2>&1)
  echo "$out" | grep '^RESULT' | sed 's/^RESULT //'
  {
    echo "## $label"; echo; echo "| 項目 | 結果 | 說明 |"; echo "|---|---|---|"
    echo "$out" | grep '^RESULT' | sed -E 's/^RESULT ([^ ]+) ([A-Z]+) ?(.*)$/| \1 | \2 | \3 |/'
    echo
  } >> "$REPORT"
  echo "$out" | grep -q '^RESULT [^ ]* FAIL' && overall=1
  for want in prereq install init up echo-roundtrip web-setup web-login web-login-reject web-room web-post \
              web-attach-25MB web-config files-upload-120MB transcript supervisor-restart doctor secret-file down; do
    echo "$out" | grep -q "^RESULT $want " || { overall=1; echo "MISSING ${want}（沒有回報結果＝視為失敗）"; echo "| ${want} | FAIL | 沒有回報結果 |" >> "$REPORT"; }
  done
  echo "$out" | grep -q '^RESULT' || { overall=1; echo "$out" | tail -20; echo "（沒有結果：$(echo "$out" | tail -2 | tr '\n' ' ')）" >> "$REPORT"; }
done
echo "結果：$REPORT"
exit $overall
