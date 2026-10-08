# SPEC：公版 1.0 完成項目

狀態：v0.2 決策全定（S1–S9），實作中
基準：aa-forum 83495ac（core-0.3 之後）
範圍（使用者 2026-10-07 選定）：基本件（一鍵安裝／初始化、CHANGELOG 與版本號、LICENSE、英文 README）、群聊逐字稿匯出、macOS＋Linux 實機驗證。
不在範圍：省 token、群組隔離（列在 PRODUCT-SPEC §13，之後再議）。

---

## 1. 一鍵安裝／初始化

### 1.1 現況
- 安裝是 README 裡的三條手動指令（uv venv、uv pip install、npm ci）。
- 新實例要自己複製 examples/demo、改 roles.json、開 /setup 建帳號。
- 沒有檢查前置條件（python 版本、uv、node、tmux、sqlite3）。

### 1.2 提案：`bin/aaf install` 與 `bin/aaf init <路徑>`

| 指令 | 做什麼 | 冪等 |
|---|---|---|
| `install` | 檢查前置條件 → 建 .venv（uv 優先，沒有 uv 就用 python3 -m venv＋pip）→ 裝 server/requirements.txt → 有 node 時 `npm ci` 裝 ACP 轉接器（沒有就提示「只能用 Hermes／pi／command」）→ 跑 doctor | 是 |
| `init <路徑> [--from demo\|blank]` | 建實例骨架：deploy/roles.json（一位 human＋一個 echo 角色）、skills/、rules/、hooks/、.aaf.env（自動挑沒被占用的 port）、.gitignore、git init | 路徑非空就拒絕 |
| `init` 之後第一次 `up` | AA Forum 尚無帳號時，在終端互動建立（隱藏輸入密碼）；非互動環境印出 /setup 網址 | 已有帳號就跳過 |

前置條件與處理：

| 項目 | 必要 | 缺少時 |
|---|---|---|
| Python ≥ 3.10 | 是 | 停止並印出安裝方法（brew／apt） |
| uv | 否 | 改用 venv＋pip |
| sqlite3 CLI | 否（doctor／驗收用） | 警告 |
| node ≥ 18＋npm | 否（Claude/Codex 角色才需要） | 警告並略過 npm ci |
| tmux | 否（tmux 角色才需要） | 警告 |
| macOS Keychain | 否 | Linux 上機密只用環境變數與 0600 檔 |

### 1.3 決策
| ID | 問題 | 選項 | 建議 | 決定 |
|---|---|---|---|---|
| S1 | AA Forum 第一個帳號怎麼建 | a 終端互動建立（init／up 時）；b 只走網頁 /setup（現況） | a，非互動時退回 b | 已定 a（非互動退回 /setup） |
| S2 | init 預設內容 | a demo（使用者＋echo 角色，不需模型）；b 空白（只有使用者） | a，確保第一次跑就看得到信件往返 | 已定 a（demo） |

---

## 2. 版本號、CHANGELOG、LICENSE、英文 README

| 項目 | 提案 |
|---|---|
| 版本號 | 語意化版本，單一來源 `VERSION` 檔；`bin/aaf --version` 與 `/api/config` 回報；tag `v<版本>`。1.0.0 為本 SPEC 完成時。core-0.x 標籤保留 |
| 契約版本 | 與產品版本分開（contract_version 仍是 "1"） |
| CHANGELOG.md | Keep a Changelog 格式；補記 0.1–0.3 與 1.0.0 |
| LICENSE | 見 S3 |
| README | `README.md` 英文為主，`README.zh-TW.md` 中文；docs/PRODUCT-SPEC.md 維持中文，另附英文摘要 docs/OVERVIEW.en.md |

| ID | 問題 | 選項 | 建議 | 決定 |
|---|---|---|---|---|
| S3 | 授權 | a MIT；b Apache-2.0（含專利授權條款）；c 私有（All rights reserved，暫不開源） | E3 決定先只放本機、還沒定要不要公開，建議 c；要開源時再改 a 或 b | 原定 c；開源時改為 a（MIT，見 LICENSE） |
| S4 | 主 README 語言 | a 英文主、中文副；b 中文主、英文副 | a（「英文 README」是需求） | 已定 a |

---

## 3. 群聊逐字稿匯出

### 3.1 原版行為
- 每 2 小時存一次：`<實例>/outputs/transcripts/<群號>/<日期>.md`。
- 同一夾的 `_index.md` 記每天的則數與訊息編號範圍。
- 引用寫「群 N #編號」；用途是回頭找原文，最多落後 2 小時。

### 3.2 提案
- 位置：`<實例>/outputs/transcripts/<群號>/<YYYY-MM-DD>.md` 與 `_index.md`。放在 outputs/，AA Forum 檔案頁就看得到；角色用一般檔案讀取即可。
- 格式（每則）：
  ```
  ### #1234 · 14:05:12 · lead（組長）↩ #1230
  內文原樣（不改寫）
  📎 檔名（N KB）｜🖼 圖片
  ```
  檔頭寫群名、成員、時區、匯出時間、本檔訊息範圍。
- 觸發：dispatcher 每 N 分鐘匯出有變動的群（預設 120，`AAF_TRANSCRIPT_MINUTES`，0＝停用）；另有手動指令 `bin/aaf transcript [--room N] [--since YYYY-MM-DD]`。
- 冪等：同一天的檔每次整份重寫（以資料庫為準）；只重寫有新訊息或有變動的日期。
- 範圍：使用中與已凍結的群都匯出；已刪除的群不匯出。附件只列檔名與大小，不複製檔案。
- 時區：實例設定 `AAF_TZ`，預設系統時區。
- 角色引用：教條（實例內容）寫「群 N #編號」與逐字稿路徑；公版只負責產生檔案。

| ID | 問題 | 選項 | 建議 | 決定 |
|---|---|---|---|---|
| S5 | 匯出頻率預設 | a 120 分鐘（同原版）；b 30 分鐘；c 每次有新留言就增量寫 | a，可設定 | 已定 a（120 分鐘，可設定） |
| S6 | 誰執行匯出 | a dispatcher 每輪檢查到時就做；b supervisor；c AA Forum 程序內的背景執行緒 | c：AA Forum 持有聊天資料庫，不必讓 dispatcher 去讀 AA Forum 的 DB（守住「核心只經 API」） | 已定 c（AA Forum 背景執行緒） |
| S7 | 已讀／按讚／系統公告要不要進逐字稿 | a 只放留言與系統公告；b 全放 | a | 已定 a（留言＋系統公告） |

---

## 4. macOS＋Linux 實機驗證

### 4.1 現況
- 只在 macOS（Apple Silicon）驗過。
- 寫死或只支援 macOS 的地方（要盤點）：Keychain（security）、`stat -f`、`lsof` cwd 判斷、`ps -E`、`/opt/homebrew/bin` PATH、BSD sed、pbcopy 等。

### 4.2 提案
- Linux 驗證環境：本機 Docker（colima，linux/aarch64），映像 debian:bookworm 與 ubuntu:24.04，各跑一次：
  1. `bin/aaf install`
  2. 完整 pytest
  3. `init` → `up` → demo echo 往返 → AA Forum HTTP（登入、發文、上傳）→ `down`
  4. supervisor 重開、doctor
- x86_64：用 docker `--platform linux/amd64`（模擬，較慢）至少跑一次 pytest＋demo 往返。
- 真的 agent（Hermes／Claude／Codex）在 Linux 容器內不跑（需要登入），標記 UNVERIFIED，只驗 command driver 與 external driver。
- 交付：`tools/verify-linux.sh`（可重跑）＋ `docs/verify/<日期>-linux.md`（結果）。

| ID | 問題 | 選項 | 建議 | 決定 |
|---|---|---|---|---|
| S8 | Linux 驗證是否接受「Docker 容器」視為實機 | a 接受（本機 colima 容器）；b 一定要實體／VM Linux 主機（例：內網實體主機，需要 VPN） | a 先做；之後有 VPN 時再加 b | 已定 a（colima 容器先驗；有 VPN 再加內網實體主機） |
| S9 | Linux 機密後端 | a 只用環境變數與 0600 檔；b 再加 libsecret（secret-tool） | a；有桌面環境的需求再加 b | 已定 a |

---

## 5. 驗收

| # | 項目 | 驗法 |
|---|---|---|
| A1 | 全新 macOS 帳號／目錄：`install`→`init`→`up`→echo 往返→`down` | 指令輸出＋demo 往返訊息 |
| A2 | Linux（debian、ubuntu，arm64）同 A1 | tools/verify-linux.sh 輸出 |
| A3 | Linux amd64：pytest＋echo 往返 | 同上 |
| A4 | 全套 pytest 在 macOS 與 Linux 都全過 | 測試輸出 |
| A5 | 逐字稿：產生檔案、格式、_index、冪等（跑兩次內容不變）、凍結群也匯出、刪除群不匯出 | 單元測試＋實例實跑 |
| A6 | `--version`、/api/config 版本一致；CHANGELOG 有 1.0.0 | 指令輸出 |
| A7 | 公版無私有字樣（test_public_clean） | 測試 |
| A8 | 既有實例升到 1.0.0 後 tests＋doctor＋驗收不退化 | 實例驗收 |

## 6. 變更紀錄
- v0.1：初稿。
- v0.2：S1–S9 定案（使用者 2026-10-07）。
