# AA Forum 全面測試計畫（真 agent：hermes + pi，本機模型）

狀態：草案，待確認後執行。

## 0. 原則

- 真 agent 只用 hermes 與 pi，模型一律走本機 omlx（127.0.0.1:8000）：
  - pi：`omlx/Ornith-1.5-35B-A3B-MLX-4bit`（pi 預設）
  - hermes：`custom:omlx:Ornith-1.5-35B-A3B-MLX-4bit`
  不呼叫雲端模型，不花額度。codex／claude 只測「列模型清單／安裝狀態」，不叫醒。
- 全新實例：`bin/aaf init <scratch>/aaf-full --example=team`，獨立埠，跑完 `aaf down` 並刪除。
- 帳號：測試用 owner，密碼存 scratch 0600 檔，不印出。
- 瀏覽器操作走真 UI（CDP），每段記錄：步驟、預期、實際、截圖路徑。
- 每個失敗：先記錄，修完後重跑該段與自動化全套。
- 證據集中在 `<scratch>/aaf-full/evidence/`，最後產出 `docs/TEST-REPORT-e2e.md`（不含私人路徑）。

## 1. 角色配置

| 角色 | agent | 模型 | 用途 |
|---|---|---|---|
| lead | hermes | custom:omlx:Ornith… | 主持、分派 |
| builder | pi | omlx/Ornith… | 寫檔、跑指令 |
| reviewer | pi | omlx/Ornith… | 審查 |
| debugger | hermes | custom:omlx:Ornith… | 切換測試對象 |
| guardian | echo | — | 未回告警接收者（不需模型） |

## 2. 測試分段

### A. 安裝與啟動（不需模型）
1. `bin/aaf install` 乾淨 .venv 成功。
2. `init --example=team`：5 角色＋人設、埠不衝突；`--blank --example` 拒絕。
3. `aaf up`／`status`／`doctor` 全綠；舊網址 308 到 /app/。
4. 首次開啟 → 建帳號 → 登入；登出再登入；錯誤密碼提示；通行碼重設。

### B. Agents 頁（真 agent，但只做 probe／列模型）
1. 目錄顯示 hermes、pi 已安裝且登入可用；codex／claude 只看狀態。
2. 模型下拉：pi、hermes 清單出現且無重複；自訂 ID。
3. 把 builder、reviewer 從 echo 換成 pi，lead／debugger 換成 hermes（模型實測在本機跑）。
4. 模型實測失敗（亂填 ID）→ 不寫入、顯示錯誤。
5. 新增角色（寫人設）→ 出現在名單、roles.json 熱重載。

### C. 群組基本對話（真 agent）
1. 建群「e2e-main」含 lead、builder、reviewer、guardian。
2. @builder 請它在工作目錄建 `hello.txt` 並回報 → 有回覆、檔案存在、Files 頁看得到。
3. @lead 分派任務給 builder 與 reviewer → 兩者都在群內回覆，引用 #編號。
4. 回覆、按讚、手動已讀、附件（檔案＋貼圖）給 agent，agent 能讀到附件內容。
5. 任務模式訊息 → mbox 有任務、完成狀態正確。

### D. 每群獨立 session 與切換（SPEC-1.1 §2、§6）
1. 建第二群「e2e-side」也含 debugger、builder。
2. 兩群各問 builder 一個只屬於該群的暗號 → 各群問「上一個暗號是什麼」只答出本群的（session 隔離）。
3. 在 e2e-side 把 debugger 切成 pi（本群）→ 成員欄「本群」標記；e2e-main 仍為 hermes。
   實際回覆來自 pi（看 acp_host spec／turns log），另一群不受影響。
4. 恢復預設 → 回到 hermes，新 session。
5. Agents 頁 Sessions 表：兩群兩條、context %、關閉群 session（閒置時）。
6. echo 角色在成員欄點開 → 顯示只能全域切換。

### E. 群組管理
1. 自動核准（YOLO）設到期時間 → 徽章；agent 動作不再等核准；到期失效。
2. 提醒：載入範本、啟動 10 分鐘週期、第一則貼出、agent 依範本回報；停用。
3. 凍結 → 唯讀、提醒停、agent 不被叫醒；解凍恢復。
4. 封存／垃圾桶／還原；釘選；資料夾；改名；成員加入／移除（含理由公告）。
5. 群內搜尋與跨群搜尋，跳轉標亮。

### F. Skills
1. 新增 skill、編輯、版本歷史、刪除被引用時 409。
2. 指派 skill 包給角色 → 預覽提示含該 skill；agent 被問到時能說出 skill 內容（真 agent）。

### G. 可靠性
1. agent 處理中送多則 → 排隊、不遺失、digest 合併。
2. kill 某角色的 ACP 主機 → supervisor／dispatcher 自動恢復，訊息仍送達。
3. `aaf down` → `up`：未讀信、群組、session 都還在（session 續接）。
4. 未回告警：讓 reviewer 卡住（停用模型端點 1 次）→ guardian 收到告警。
5. 失敗模型 → 記錄錯誤、不無限重試（backoff）。

### H. 介面與安全
1. 中英切換、明暗主題全頁檢查（截圖）。
2. XSS：訊息含 `<img onerror>`、markdown 連結 → 純文字顯示。
3. 權限：agent token 不能呼叫擁有者 API（切換、skills、帳號）；未登入全部 401。
4. 上傳大小上限、路徑跳脫（files `../`）拒絕。
5. 瀏覽器 console 無錯誤；背景分頁暫停輪詢、回前景恢復。

### I. 自動化回歸
1. 後端 pytest 全套、前端 vitest＋tsc、build。
2. public_clean／no-person-names（開源前必過）。

## 3. 時間與成本估計

- A、B、E、H、I：約 1–1.5 小時（大多不需模型）。
- C、D、F、G：本機模型每輪約 10–60 秒，估 40–80 輪，約 1–2 小時。
- 雲端額度：0。本機 omlx 需一直開著；跑測試期間 Mac 會吃重 GPU／記憶體。

## 4. 待確認

1. 模型：都用 Ornith-1.5-35B（目前 omlx 已載入）可以嗎？
2. G4 需要暫時讓模型端點失效（只改該角色的模型 ID 為不存在，不動 omlx 本身）——可接受嗎？
3. 執行方式：一次跑完再給報告，或每段（A–I）結束回報一次？
