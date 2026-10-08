# 契約（contract）

核心（溝通層、執行層）與 driver 之間唯一的介面。核心只認得這裡的格式，不認得任何 agent。

| 檔案 | 用途 |
|---|---|
| v1/wake_request.schema.json | 叫醒一輪的輸入 |
| v1/turn_result.schema.json | 一輪結束的結果（核心記錄於 turn_results 表；AA Forum 血量的資料來源） |
| v1/manifest.schema.json | driver 自我描述：名稱、版本、能力旗標、支援的叫醒模式 |
| v1/usage.schema.json | 選配：context／模型等用量（driver 可讀自家 agent 內部資料，只住 driver） |

版本規則：
- 同主版號內只新增「選填」欄位，不改既有欄位意義、不刪欄位。
- 破壞性變更升主版號（v2/），核心同時支援新舊一版。
- `contract_version` 欄位必填，值為主版號字串。

驗證：`mbox.contract.validate(kind, obj)`；測試會對每個 driver 的輸入輸出強制驗證。
