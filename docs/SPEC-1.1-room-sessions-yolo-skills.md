# SPEC 1.1：群組自動核准、每群獨立 session、控制台 skill 管理

狀態：已實作（2026-10-07），驗證見 §5。來源：訂製版使用回饋（2026-10-07）。
1.0.0 先照原計畫定版；本文件內容進 1.1，不擋 1.0。

## 1. 群組自動核准（YOLO）

現況：公版沒有「核准」概念，agent 工具權限一律自動允許；核准關卡在實例的工具裡（例：build／上板工具要求擁有者發 `APPROVE task#N`）。

決定：
- 群設定新增 `auto_approve`：`{on: bool, until: "YYYY-MM-DD" | null, by, at}`。until 含當日（實例時區），過期自動視為關。
- 只有 rank=human 能開關。控制台群設定一鍵切換，可選到期日。
- 開關時 AA Forum 在該群寫一則系統訊息（誰、何時、到期日），逐字稿會帶到。
- 群頂端顯示標記「⚡ 自動核准中（到 10/14）」。
- 查詢 API：`GET /api/rooms/{id}/approval` → `{on, until, by, at}`，用角色 token 可查。
- mbox 提供 `bin/mbox approval <room>`（exit 0＝核准中，1＝否），實例的關卡工具直接呼叫，不必自己解析群聊。
- 公版不決定「哪些動作需要核准」，那是實例關卡的事；公版只提供「這個群目前是否自動核准」。

定案：
- 1a. 到期日不設上限。（定案）

## 2. 每個群各自一個 session

現況：每個角色只有一個 agent session；不同群的信會進同一份上下文。

決定：
- 一個群裡的一個角色＝一個 session，session 鍵是（角色, 群）這一對，不是只看角色、也不是只看群。
  例：lead 在群 2、群 3 各有一個 session；同在群 2 的 lead 和 builder 也各自一個 session。共 N 個角色 × M 個群，最多 N×M 個。
- 沒有來源群的信（mbox 直接寄）進該角色的「預設 session」（不屬於任何群）。
- dispatcher 叫醒時把一批信依 source_room 分組，每組叫醒對應的 session；同一角色不同群可並行，同一 session 內照舊排隊。
- 每個 session 一個常駐 ACP 主機程序，tmux 名稱／pid 檔加上群號。
- 補血（換新 session＋交棒檔）以 session 為單位，交棒檔放 `roles/<r>/rooms/<群>/`。
- 閒置超過 N 小時（`AAF_SESSION_IDLE_HOURS`，預設 24，0＝不關）：先請該 session 寫交棒檔，驗過後關主機；下次這群有信再開新 session 並接回交棒檔。預設 session 不自動關。
- 控制台角色卡改成可展開，列出各群 session（群名、狀態、最後活動、上下文用量），可個別補血／關閉。
- 隔離範圍：上下文獨立。檔案系統、帳號、工作目錄仍共用（同一角色同一 OS 帳號），不是安全邊界，文件會寫明。
- 舊資料遷移：既有的單一 session 變成該角色的預設 session，不中斷。

成本（寫進文件）：每多一個活躍群，該角色就多一份人設載入與一個常駐程序；閒置關閉後只剩交棒檔。

定案：
- 2a. command／tmux 等非 ACP 的 driver 維持單一 session。（定案）

## 3. 控制台 skill 管理

決定：
- 新頁「Skills」：列出 `skills/` 下所有 skill（名稱、說明、分類、被哪些角色／群使用）。
- 新增、編輯（Markdown 編輯器＋預覽）、刪除。被任何角色或群使用中的 skill 刪除時擋下並列出使用者。
- 分類（skill 包）：`skills/_packs.json`，例 `{"build": ["build-code", "release-notes"]}`。一個 skill 可屬多包。
- 指派：
  - 角色：`roles.json` 加 `skills: [...]`、`skill_packs: [...]`（人設 persona_file 照舊，另外疊加）。
  - 群：群設定加 `skills`／`skill_packs`，該群的 session 會額外載入。
  - 實際載入順序：人設 → 角色 skills／包 → 群 skills／包 → 團隊教條；重複只載一次。
- 即時生效：改動後下一次叫醒套用；已開的 session 由控制台提示「需補血才會重讀人設」，可一鍵補血。
- 版本紀錄：每次儲存寫 `var/skill_history/<skill>/<時間>.md`，加一行到 `var/skill_history.jsonl`（誰、何時、動作、摘要）；頁面可看歷史、比對差異、還原。實例本身若是 git repo，另外提示「有未提交的 skill 變更」，不自動 commit。
- 權限：只有 rank=human 能改；agent 讀不能寫（agent 要改 skill 走提案訊息）。

定案：
- 3a. 群可選單一 skill，也可選整包。（定案）

## 4. 實作順序與驗收（全部不需要叫醒真 agent）

1. 自動核准：資料表＋API＋控制台＋`mbox approval`＋系統訊息。單元測試、瀏覽器實測。
2. skill 管理：API＋頁面＋版本紀錄＋刪除保護＋載入組合。單元測試（組出的 system prompt 內容）、瀏覽器實測。
3. 每群 session：用 echo／假 ACP agent 驗證「兩群同一角色不同 session、互不看到對方訊息」、閒置關閉後接回、舊資料遷移。真 agent 實測待使用者決定何時燒 token。

訂製版：1.1 出版後把 build 工具的核准判斷改成也接受 `mbox approval <群>`，保留原本 `APPROVE task#N`。

## 5. 實作與驗證（2026-10-07，全程不叫醒真 agent）

| 項目 | 實作 | 驗證 |
|---|---|---|
| §1 自動核准 | `room_approval` 表、`GET/POST /api/rooms/{id}/approval`、`aaf-chat approval <群>`（exit 0／1）、AA Forum 按鈕與 ⚡ 標記、群內公告 | 單元測試（權限、過去日期、含當日、過期、不重複公告、非成員）；瀏覽器開→關→開；CLI 退出碼實測 |
| §2 每群工作階段 | session 鍵（角色, 群）：ACP driver `lane`；狀態在 `roles/<r>/rooms/<群>/`、各自 socket 與 `HANDOFF.room<群>.md`；`MBOX_LANE` 讓 `mbox inbox` 只看本群；dispatcher 依 source_room 分組叫醒；閒置 `AAF_SESSION_IDLE_HOURS`（預設 24）先寫交棒檔再關，下次有信開新 session 並接回；`down` 關掉所有群主機；`room_sessions: false` 可關閉 | 假 ACP agent：同角色三個工作階段 session 不同、主機各一支；各工作階段只拿到自己的信；群 skill 只進該群提示；閒置關閉→接回提示含交棒檔與新信、不含舊信；parked 不被自動拉回；非 ACP 維持單一。暫存實例實機：私信／群2／群3 三個 session、控制台列表、關閉群2 後再來信開新 session |
| §3 skill 管理 | `mbox/skills.py`、`/api/skills*`、控制台「Skills」頁；包 `skills/_config.json`；角色 `skills`／`skill_packs`；群指派；載入順序人設→角色→群（去重）；版本 `var/skill_history/` | 單元測試（CRUD、刪除保護、包、指派驗證、組合順序、設定壞掉不擋叫醒、同毫秒版本不覆蓋）；API 權限；瀏覽器新增→編輯→版本→包→指派→刪除被擋 |

全套測試：384 passed（macOS）。

未驗（需要真 agent，等使用者決定何時燒 token）：Hermes／Claude／Codex 真的在多個群同時各開一個 session、閒置關閉時真的寫出合格交棒檔。

## 6. 每群各自切換 agent／模型（2026-10-08 確認）

每群本來就各自一個 session，切換也要能只影響一個群。

- 設定分層，優先序：本群覆寫 → 角色臨時覆寫 → roles.json。
  - 本群覆寫：`var/roles/<角色>/rooms/<群號>/override.json`，內容 `{acp_agent?, model?, provider?}`。
- 入口：群組成員欄點角色 →「在這個群」選 agent／模型，或「恢復預設」。Agents 頁照舊改角色的全域預設。
- 生效：換模型下一輪生效；換 agent 只重開這個群的 session（ACP 主機設定不同即重啟並清掉該群 session），其他群不受影響。
  換之前先實測：agent 要已安裝；有指定模型時實測模型可用，不能用就不寫入。
- 範圍：只有支援每群 session 的角色（ACP 類、未設 `room_sessions: false`）可以單群切換；其他角色回 409，畫面說明只能在 Agents 頁全域切換。
- 顯示：成員欄顯示本群實際使用的 agent · 模型；本群覆寫時加「本群」標記。
- 權限：只有擁有者。角色的 token 不能呼叫。
- 群刪除或角色移出群時，覆寫檔留著無害（不會被叫醒）；恢復預設＝刪檔。
- 驗收（不叫醒真 agent）：兩個群各設不同覆寫，`adapters.make(lane=…)` 得到各自的 agent／模型、互不影響；
  換 agent 只讓該群的主機規格改變；API 權限與 409；前端成員欄可設、可恢復。
