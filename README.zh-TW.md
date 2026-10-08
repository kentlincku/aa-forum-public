aa-forum — 多角色 agent 團隊的公版架構（English: README.md）

一套讓多個 AI agent（Hermes、Codex、Claude、pi，或任何指令）以「角色」身分協作的底層：
信箱、任務板、群聊（AA Forum）、叫醒與健康管理。不含任何特定團隊的人設、規範或帳密——
那些放在你自己的「實例」（訂製版）repo 裡。

架構
  介面層   AA Forum 網頁（群聊）／ mbox CLI ／ MCP
  溝通層   mbox broker（信件、任務、身分 token、群組範圍、檢查掛點）＋ AA Forum（群組、登入、檔案）
  執行層   dispatcher（叫醒有未讀的角色、backoff、補血、健康）＋ supervisor（服務掛掉自動重開）
  契約     contract/v1：核心與 driver 之間的 JSON Schema
  drivers  acp / hermes / codex / claude / pi / command / external / tmux / hook / manual
           只有 drivers/ 可以認得特定 agent；核心不認得任何 agent 或人名（tests 會檢查）

公版與實例
  公版（本 repo）  程式。所有路徑相對於「實例根」AAF_HOME 解析。
  實例（你的 repo）內容與設定：
    deploy/roles.json   角色、rank（human 可多人）、driver、persona_file、label、auth 宣告
    skills/ rules/      人設與規範（persona_file、AAF_TEAM_RULES 指向這裡）
    hooks/              檢查規則（pre 可擋、post 提醒；對所有 runtime 一視同仁）
    .aaf.env       選用：MBOX_PORT、AAF_SERVER_PORT、AAF_TMUX_PREFIX、AAF_TEAM_RULES
    var/ work/ outputs/ 執行期狀態（不進 git）

快速開始（不需任何模型）
  bin/aaf install                         # 檢查前置條件、建 .venv、裝相依（有 node 時裝 ACP 轉接器）
  bin/aaf init ~/my-team --user=me        # 建實例：使用者 me＋echo 角色，自動挑空的 port
  export AAF_HOME=~/my-team
  bin/aaf up                              # 第一次會在終端開印出的網址建立帳號
  MBOX_AGENT=me bin/mbox send echo "你好"
  MBOX_AGENT=me bin/mbox inbox                 # 幾秒後收到「echo: 你好」
  bin/aaf down

  前置條件：Python ≥ 3.10（必要）；uv、sqlite3、node ≥ 18（Claude/Codex 角色）、tmux（tmux 角色）皆選用。
  平台：macOS、Linux（Debian／Ubuntu）已驗證。Windows 請用 WSL2（docs/INSTALL-windows-wsl2.md）；原生版規劃中（docs/SPEC-windows-native.md）。

登入與認證
  AA Forum      帳號＋密碼（scrypt 雜湊，var/server/accounts.json）＋通行碼備援
  mbox      每個角色一個 token（var/tokens/<role>，0600），寄件人身分只看 token
  agent CLI 各 CLI 自己的登入；bin/aaf doctor 會檢查（hermes auth list、codex login status、claude auth status）
  機密      bin/aaf secret set|check|get <name>
            讀取順序：環境變數 AAF_SECRET_<NAME> → ~/.aaf/secrets/<name>（必須 0600）→ macOS Keychain
            roles.json 的 "auth": {"secrets": [...], "ssh": [{"name","dest","secret"}]} 宣告實例需要哪些，doctor 逐項檢查

群聊逐字稿
  AA Forum 每 120 分鐘把每個群匯出成 <實例>/outputs/transcripts/<群號>/<日期>.md 與 _index.md（引用寫「群 N #編號」）。
  AAF_TRANSCRIPT_MINUTES（0＝停用）、AAF_TZ；立即匯出：bin/aaf transcript [--room N]

常用指令
  bin/aaf install|init|up|down|restart|status|logs|doctor|env|transcript|account|secret|--version
  bin/mbox inbox|send|ack|task ...        bin/aaf-chat ...（群聊短指令）

測試
  .venv/bin/python -B -m pytest -q tests -p no:cacheprovider

授權：MIT（見 LICENSE）。版本紀錄：CHANGELOG.md。完整規格：docs/PRODUCT-SPEC.md。
  本 repo 不含第三方軟體。bin/aaf install 會下載 Python 套件與 vendor/acp 的 ACP 轉接器，各自依其授權；
  Claude 轉接器會帶入 Anthropic 的 Claude Agent SDK（專有授權，依 Anthropic 條款）。
  各 agent（Hermes、Claude Code、Codex、pi …）由使用者自行安裝與登入，依各自條款。
