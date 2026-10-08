# 由 bin/aaf、bin/mbox、bin/aaf-chat 共用（以 . 載入）：決定實例根並讀 .aaf.env。
# 只接受白名單 KEY=VALUE，不執行任何 shell；已在環境中設定的值優先。
# 需要呼叫端先設 ROOT（公版根）。
# 公版用自己的 .venv：外部的 PYTHONPATH／PYTHONHOME 會讓它載入別的環境的套件（實測：jsonschema 載到別處的 rpds 而失敗）。
unset PYTHONPATH PYTHONHOME
AAF_HOME="$(cd "${AAF_HOME:-$ROOT}" 2>/dev/null && pwd -P)" || { echo "AAF_HOME 不存在" >&2; exit 1; }
export AAF_HOME
if [ -f "$AAF_HOME/.aaf.env" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    k=${line%%=*}; v=${line#*=}
    case "$k" in
      AAF_*|MBOX_PORT|MBOX_URL)
        eval "cur=\${$k:-}"
        [ -z "$cur" ] && export "$k=$v" ;;
    esac
  done < "$AAF_HOME/.aaf.env"
fi
export MBOX_HOME="${MBOX_HOME:-$AAF_HOME/var}"
export MBOX_PORT="${MBOX_PORT:-8775}"
export MBOX_URL="${MBOX_URL:-http://127.0.0.1:${MBOX_PORT}}"
export AAF_SERVER_PORT="${AAF_SERVER_PORT:-8111}"
export AAF_SERVER_STATE="${AAF_SERVER_STATE:-$MBOX_HOME/server}"
export AAF_CHAT_URL="${AAF_CHAT_URL:-http://127.0.0.1:${AAF_SERVER_PORT}}"
