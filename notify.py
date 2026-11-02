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


def format_weverse_comment(comment: dict, zh_body: str | None, post_url: str,
                           zh_reply_to: str | None = None, show_reply_to: bool = False,
                           limit: int | None = None) -> str:
    """粉絲（或成員）：被回覆的留言 / 成員 回覆：留言；沒回覆別人就 成員：留言。

    limit（Threads 500 字）：超過時先縮被回覆的那句，還不夠才縮成員留言，標題、連結、註記保留。
    """
    author = comment["author"]
    rt = comment.get("reply_to") if show_reply_to else None
    body = zh_body or comment["body"]
    quoted = (zh_reply_to or rt["body"]) if rt else ""

    def build(q: str, b: str) -> str:
        lines = [f"↪ {author} 在 Weverse 留言了 💬"]
        if rt:
            lines.append(f"{rt['author'] or '粉絲'}：{q}")
            lines.append(f"{author} 回覆：{b}")
        else:
            lines.append(f"{author}：{b}")
        lines.append(f"▶ {post_url}")
        if zh_body:
            lines.append(AI_NOTE)
        return "\n".join(lines)

    text = build(quoted, body)
    if not limit or len(text) <= limit:
        return text
    if rt:  # 先縮被回覆的那句，至少留「…」
        quoted = _clip(quoted, max(1, limit - len(build("", body))))
        text = build(quoted, body)
    if len(text) > limit:  # 還不夠：縮成員留言
        body = _clip(body, max(1, limit - len(build(quoted, ""))))
        text = build(quoted, body)
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
