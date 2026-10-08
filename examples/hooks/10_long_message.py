"""範例規則：群文或私訊超過 1500 字要附理由（仿原版群聊七條第 4 條）。

複製到 $AAF_HOOKS（預設 <專案>/hooks/）即生效；不需重啟（依檔案修改時間重新載入）。
"""
LIMIT = 1500
TAG = "# long-ok:"


def pre(event):
    body = event.get("body") or ""
    if len(body) <= LIMIT or event.get("rank") == "human":
        return None
    i = body.find(TAG)
    if i >= 0 and len(body[i + len(TAG):].split("\n", 1)[0].strip()) >= 6:
        return None
    return (f"內容 {len(body)} 字，超過 {LIMIT}。請把細節寫成檔案、訊息只放路徑；"
            f"真的必須長，在內文加一行 `{TAG}<至少 6 字的理由>`。")


def post(event):
    if event.get("type") == "chat.post" and "\n- 大意：" not in "\n" + (event.get("body") or ""):
        return "群文開頭兩行建議寫「- 對應：…」「- 大意：…」，讓讀者知道在講哪件事。"
    return None
