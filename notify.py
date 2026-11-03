"""推文文字格式（純文字，TG／Threads 共用）＋ Telegram 推播。"""
import html
import os

import requests

API = "https://api.telegram.org/bot{token}/{method}"
AI_NOTE = "本文由 AI 自動翻譯上傳"  # 有譯文才加


def _call(method: str, **payload) -> dict:
    url = API.format(token=os.environ["TELEGRAM_BOT_TOKEN"], method=method)
    resp = requests.post(url, json=payload, timeout=20)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} 失敗：{data.get('description')}")
    return data["result"]


def send_admin(text: str) -> None:
    """系統警示發到使用者個人聊天室（.env 的 TELEGRAM_ADMIN_CHAT_ID）；沒設就只寫 log。"""
    chat_id = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
    if not chat_id:
        return
    _call("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True)


# ───── Threads 審核用（review.py）：私訊預覽＋按鈕、收回覆 ─────
def send_review(text: str, uid: str) -> int:
    kb = {"inline_keyboard": [[
        {"text": "✅ 發佈", "callback_data": f"th:pub:{uid}"},
        {"text": "✍️ 加評論", "callback_data": f"th:cmt:{uid}"},
        {"text": "⏭ 略過", "callback_data": f"th:skip:{uid}"},
    ]]}
    return _call("sendMessage", chat_id=os.environ["TELEGRAM_ADMIN_CHAT_ID"], text=text[:MESSAGE_LIMIT],
                 reply_markup=kb, disable_web_page_preview=True)["message_id"]


def edit_review(message_id: int, status_line: str) -> None:
    """拿掉按鈕，狀態用回覆接在預覽下面。"""
    chat_id = os.environ["TELEGRAM_ADMIN_CHAT_ID"]
    _call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
    _call("sendMessage", chat_id=chat_id, text=status_line,
          reply_parameters={"message_id": message_id, "allow_sending_without_reply": True})


def ask_comment(text: str) -> int:
    return _call("sendMessage", chat_id=os.environ["TELEGRAM_ADMIN_CHAT_ID"], text=text,
                 reply_markup={"force_reply": True, "input_field_placeholder": "你的評論"})["message_id"]


def answer_callback(callback_id: str, text: str) -> None:
    _call("answerCallbackQuery", callback_query_id=callback_id, text=text)


def get_updates(offset: int | None, timeout: int = 0) -> list[dict]:
    url = API.format(token=os.environ["TELEGRAM_BOT_TOKEN"], method="getUpdates")
    payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    resp = requests.post(url, json=payload, timeout=timeout + 15)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates 失敗：{data.get('description')}")
    return data["result"]


def format_post(artist: str, video: dict, zh_title: str | None, hashtags: list[str] | None = None) -> str:
    """YouTube 影片推文（純文字）。"""
    kind = "新 Shorts" if video.get("is_short") else "新影片"
    lines = [f"{artist} 發佈了{kind}！🎬"]
    lines.append(f"「{zh_title or video['title']}」")  # 只放譯文；翻譯失敗才用原標題
    lines.append(f"▶ {video['url']}")
    if hashtags:
        lines.append(" ".join(hashtags))
    if zh_title:
        lines.append(AI_NOTE)
    return "\n".join(lines)


def send_video(artist: str, video: dict, zh_title: str | None, hashtags: list[str] | None = None) -> None:
    text = html.escape(format_post(artist, video, zh_title, hashtags))
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    if video.get("thumbnail"):
        try:
            _call("sendPhoto", chat_id=chat_id, photo=video["thumbnail"], caption=text, parse_mode="HTML")
            return
        except RuntimeError:
            pass  # 縮圖失敗就改送純文字
    _call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


def format_weverse_post(artist: str, post: dict, zh_body: str | None, hashtags: list[str] | None = None,
                        max_len: int = MESSAGE_LIMIT, more: str = "…", platform: str = "Weverse") -> str:
    """Weverse／Instagram 成員貼文（純文字）。只放譯文；翻譯失敗或不用翻才用原文。超過 max_len 截內文並接 more。"""
    member = "（會員限定）" if post.get("membership_only") else ""
    if post.get("kind") == "notice":
        head = f"{artist} 官方在 {platform} 發佈了公告！📢"
    else:
        head = f"{artist} {post['author']} 在 {platform} 發佈了新貼文！{'📷' if platform == 'Instagram' else '💬'}{member}"
    tail = [f"▶ {post['url']}"]
    if post.get("has_video"):
        tail.insert(0, "（貼文含影片，請點連結觀看）")
    if hashtags:
        tail.append(" ".join(hashtags))
    if zh_body:
        tail.append(AI_NOTE)
    body = zh_body or post["body"]

    def build(b: str) -> list[str]:
        return [head, "", b, ""] + tail if b.strip() else [head, ""] + tail

    plain = "\n".join(build(body))
    if len(plain) > max_len:
        body = body[: max(0, len(body) - (len(plain) - max_len) - len(more))] + more
        plain = "\n".join(build(body))
    return plain


def split_threads_post(artist: str, post: dict, zh_body: str | None, hashtags: list[str] | None,
                       platform: str, limit: int) -> list[str]:
    """Threads 版：超過 limit 不截斷，切成多段，第 2 段起當前一段的回覆（留言接續）。
    第 1 段：標題＋內文開頭＋hashtag（主題標籤要在主貼文）；最後一段結尾接連結、AI 註記；中間段只有內文。"""
    full = format_weverse_post(artist, post, zh_body, hashtags, max_len=10**9, platform=platform)
    if len(full) <= limit:
        return [full]
    head, _, rest = full.partition("\n\n")
    body, _, tail = rest.rpartition("\n\n")  # tail = ▶ 連結／含影片／hashtag／AI 註記
    tag_line = " ".join(hashtags) if hashtags else ""
    tail = "\n".join(l for l in tail.split("\n") if l != tag_line)
    first_fixed = len(head) + 2 + (len(tag_line) + 2 if tag_line else 0)
    parts, cur, cap = [], "", limit - first_fixed - 6  # -6 給「（續）」記號
    for line in body.split("\n"):
        while len(line) > cap:  # 單行就超長：硬切
            parts.append(cur + line[:cap]); cur, line = "", line[cap:]; cap = limit - 12
        if len(cur) + len(line) + 1 > cap:
            parts.append(cur.rstrip("\n")); cur = ""; cap = limit - 12
        cur += line + "\n"
    parts.append(cur.rstrip("\n"))
    parts = [p for p in parts if p.strip()]
    n = len(parts)
    out = []
    for i, p in enumerate(parts):
        s = f"{head}\n\n{p}" if i == 0 else f"（續 {i + 1}/{n}）\n{p}"
        if i == 0 and tag_line:
            s += f"\n\n{tag_line}"
        if i == n - 1:
            s += f"\n\n{tail}"
        out.append(s[:limit])
    return out


def send_weverse_post(artist: str, post: dict, zh_body: str | None, hashtags: list[str] | None = None,
                      platform: str = "Weverse") -> int:
    """回傳帶內文那則訊息的 message_id（之後成員留言用回覆掛在它下面）。"""
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    photos = post.get("photos", [])[:10]
    plain = format_weverse_post(artist, post, zh_body, hashtags, platform=platform)
    text, length = html.escape(plain), len(plain)
    if not photos:
        return _call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)["message_id"]
    media = [{"type": "photo", "media": u} for u in photos]
    if length <= CAPTION_LIMIT:
        media[0].update(caption=text, parse_mode="HTML")
        return _call("sendMediaGroup", chat_id=chat_id, media=media)[0]["message_id"]
    _call("sendMediaGroup", chat_id=chat_id, media=media)
    return _call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)["message_id"]


def _clip(text: str, n: int) -> str:
    """截到 n 字以內，截到的話尾巴放「…」。"""
    if len(text) <= n:
        return text
    return text[: n - 1] + "…" if n > 0 else ""


def format_weverse_comment(comments: list[dict], zh_bodies: list[str | None],
                           zh_reply_to: str | None = None, show_reply_to: bool = False,
                           limit: int | None = None) -> str:
    """粉絲（或成員）：被回覆的留言 / 成員 回覆：留言；沒回覆別人就 成員：留言。

    comments 是同一位成員對同一則留言的回覆（同一輪合併，依時間排序）；多則時被回覆那句只出現一次，
    回覆每句一行「· 」。
    不放連結（使用者 2026-11-03 決定；TG／Threads 都掛在原貼文下面，看得到出處）。
    limit（Threads 500 字）：超過時先縮被回覆的那句，還不夠從最早的回覆開始縮，標題、註記保留。
    """
    first = comments[0]
    author = first["author"]
    rt = first.get("reply_to") if show_reply_to else None
    bodies = [zh or c["body"] for c, zh in zip(comments, zh_bodies)]
    quoted = (zh_reply_to or rt["body"]) if rt else ""
    has_zh = any(zh_bodies)

    def build(q: str, bs: list[str]) -> str:
        lines = [f"↪ {author} 在 Weverse 留言了 💬"]
        if rt:
            lines.append(f"{rt['author'] or '粉絲'}：{q}")
        label = f"{author} 回覆" if rt else author
        if len(bs) == 1:
            lines.append(f"{label}：{bs[0]}")
        else:
            lines.append(f"{label}：")
            lines += [f"· {b}" for b in bs]
        if has_zh:
            lines.append(AI_NOTE)
        return "\n".join(lines)

    text = build(quoted, bodies)
    if not limit or len(text) <= limit:
        return text
    if rt:  # 先縮被回覆的那句，至少留「…」
        quoted = _clip(quoted, max(1, limit - len(build("", bodies))))
        text = build(quoted, bodies)
    for i in range(len(bodies)):  # 還不夠：從最早的回覆開始縮
        over = len(text) - limit
        if over <= 0:
            break
        bodies[i] = _clip(bodies[i], max(1, len(bodies[i]) - over))
        text = build(quoted, bodies)
    return text[:limit]


def send_weverse_comment(text: str, reply_to_message_id: int | None) -> int:
    """text 是 format_weverse_comment 的純文字。"""
    text = html.escape(text)
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    extra = {}
    if reply_to_message_id:
        extra["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
    return _call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                 disable_web_page_preview=True, **extra)["message_id"]
