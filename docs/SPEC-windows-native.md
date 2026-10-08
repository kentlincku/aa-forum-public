# SPEC：原生 Windows 版（規劃，未實作）

狀態：草案，逐項確認後才實作。Windows 目前以 WSL2 支援（[INSTALL-windows-wsl2.md](INSTALL-windows-wsl2.md)）。

## 0. 目標與原則

- 不裝 WSL 也能在 Windows 10/11 上安裝、啟動、用 Windows 本機已裝好的 agent。
- macOS／Linux 行為不變：每項改動以「抽一層平台介面」做，現有 POSIX 路徑保持原樣，既有測試全過。
- 不做：tmux 互動模式、主機系統帳號登入（os_auth）在 Windows 先不支援，介面說明原因。

## 1. 現況盤點（依原始碼掃描）

| 項目 | 現況 | 位置 |
|---|---|---|
| 指令入口 | bash 腳本 | `bin/aaf`、`bin/aaf-setup`、`bin/aaf-chat`、`bin/mbox`、`bin/mbox-stop-hook`、`bin/_instance_env.sh` |
| ACP 主機控制通道 | Unix socket（`AF_UNIX`） | `drivers/acp.py`、`drivers/acp_host.py` |
| 程序群組與停止 | `start_new_session`、`os.killpg`、`SIGTERM/SIGKILL` | `drivers/acp*.py`、`drivers/base.py`、`drivers/agy_acp.py`、`tools/acp_probe.py` |
| 背景常駐／停止 | `nohup`、pid 檔、`kill` | `bin/aaf` |
| 機密檔權限 | `chmod 0600`／`umask` | `mbox/cli.py`、`mbox/dispatcher.py`、`mbox/doctor.py`、`mbox/secret_store.py`、`server/app.py`、`drivers/acp_host.py` |
| 系統帳號 | `import pwd` | `server/os_auth.py`、`server/app.py`、`mbox/doctor.py` |
| 示範 agent | sh 腳本 | `examples/demo/bin/echo-agent`、`examples/external-driver/echo-driver.sh` |
| tmux driver | tmux | `drivers/base.py`（Tmux、Hooked） |

不需改：FastAPI 後端、SQLite、前端靜態檔（已建置）、ACP adapter（Node）。

## 2. 設計（待逐項確認）

### 2.1 指令入口改 Python（決策 W1）
- 新增 `aaf/cli.py`（Python）實作 install／init／up／down／status／doctor／account／transcript；
  `bin/aaf` 改成薄殼呼叫它；Windows 提供 `aaf.cmd`／`aaf.ps1` 同樣呼叫它。
- `_instance_env.sh` 的 `.aaf.env` 白名單載入移到 Python（`mbox/paths.py` 已有一份，合併成單一實作）。
- 建議：一次改掉，兩套平台共用 Python 入口（避免 bash 與 PowerShell 雙份邏輯）。

### 2.2 ACP 主機控制通道（決策 W2）
- 選項 A：本機 TCP `127.0.0.1:<隨機埠>` ＋ 每次啟動產生的 token（存在 state_dir，僅本人可讀）。
- 選項 B：Windows named pipe（`\\.\pipe\aaf-<role>-<room>`），POSIX 照用 Unix socket。
- 建議 A：兩平台同一份程式；token 擋掉同機其他使用者。

### 2.3 程序管理（決策 W3）
- 抽 `platform/proc.py`：`spawn_group(argv)`、`kill_group(pid, hard)`、`alive(pid)`。
  - POSIX：沿用 `start_new_session` + `killpg`。
  - Windows：`CREATE_NEW_PROCESS_GROUP` + Job Object（關閉時整組結束）；退路 `taskkill /T /F`。
- `bin/aaf up/down` 的常駐改由 Python supervisor 啟動（`DETACHED_PROCESS`），pid 檔照舊。

### 2.4 機密檔權限（決策 W4）
- 抽 `platform/secure.py`：`write_private(path, data)`、`check_private(path)`。
  - POSIX：0600。
  - Windows：建檔後以 ACL 限縮為目前使用者（`icacls` 或 pywin32）；doctor 檢查改為 ACL 檢查。

### 2.5 不支援項目
- `os_auth`：Windows 回「不支援」，只用帳號密碼登入。
- tmux driver：Windows 不註冊；角色設 tmux 時 doctor 報錯並說明。

### 2.6 其他
- 示範 echo-agent 改 Python（兩平台共用）。
- 主控台編碼：所有子程序 I/O 明確 UTF-8（`PYTHONUTF8=1`、`encoding='utf-8'`），避免 BIG5／cp950。
- 路徑一律 `pathlib`；檔名比對不分大小寫的情況（Windows）要測。
- 換行：讀 roles.json／SKILL.md 時接受 CRLF。

## 3. 驗收（Windows 主機實測，不叫醒雲端模型）

1. 全新 Windows 11：`aaf install` → `aaf init --example=team` → `aaf up`，瀏覽器可開。
2. pi／hermes（Windows 本機安裝）經 ACP 對話；兩群 session 隔離；單群切換。
3. 強制結束 agent 程序 → 自動恢復；`aaf down` 後沒有殘留程序。
4. 機密檔 ACL 只剩本人；doctor 全綠。
5. macOS、Linux 既有測試全過；CI 加 windows-latest 跑單元測試。

## 4. 估計

依賴順序：W1 → W3 → W2 → W4 → 其他。粗估 3–5 個工作天，主要在 W2、W3。確認決策後再細估。

## 5. 待確認

- W1 指令入口全面改 Python？
- W2 用本機 TCP＋token（A）還是 named pipe（B）？
- W3 Job Object 需要 pywin32 依賴（Windows 才裝），可以嗎？
- W4 ACL 用 `icacls`（不加依賴）還是 pywin32？
