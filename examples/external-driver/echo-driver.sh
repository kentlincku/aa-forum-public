#!/bin/sh
# 外部 driver 範例（契約 v1）：不呼叫任何模型，只把 prompt 長度回報成一輪。
# 用法：roles.json 角色設 {"driver": "external", "exec": "<本檔絕對路徑>"}
# 需要 python3 解析 JSON（任何語言都可以，只要遵守 stdin/stdout 契約）。
case "$1" in
  manifest)
    printf '%s\n' '{"name":"echo-driver","version":"0.1.0","contract_version":"1","capabilities":["usage_report"],"wake_modes":["headless"]}' ;;
  check)
    command -v python3 >/dev/null && echo "echo-driver ok" || { echo "缺 python3"; exit 1; } ;;
  wake)
    python3 -c '
import json, sys, time, uuid
req = json.load(sys.stdin)
sid = req.get("session_id") or "echo-" + uuid.uuid4().hex[:8]
print("echo-driver 收到", req["role"], file=sys.stderr)
print(json.dumps({"contract_version": "1", "role": req["role"], "driver": "echo-driver", "exit": 0, "ok": True,
                  "session_id": sid, "text": "收到 %d 字" % len(req["prompt"]),
                  "model_used": req.get("model") or "echo-0", "tokens": {"input": len(req["prompt"]), "output": 3}}))
' ;;
  usage)
    printf '%s\n' '{"contract_version":"1","model":"echo-0","context_used":10,"context_limit":100,"percent":10,"source":"echo-driver 固定值"}' ;;
  *) echo "未知子指令 $1" >&2; exit 2 ;;
esac
