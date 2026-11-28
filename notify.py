"""推文文字格式（純文字，TG／Threads 共用）＋ Telegram 推播。"""
import html
import io
import json
import logging
import os
import signal
import threading
from contextlib import contextmanager

import requests

API = "https://api.telegram.org/bot{token}/{method}"


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


def send_admin_html(text: str) -> None:
    """同 send_admin，但 parse_mode=HTML（統整完的大綱＋爆點列表，picks_msg.py）。"""
    chat_id = os.environ.get("TELEGRAM_ADMIN_CHAT_ID")
    if not chat_id:
        return
    _call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)


# ───── Threads 審核用（review.py）：私訊預覽＋按鈕、收回覆 ─────
REVIEW_TOGGLES = (("nohead", "去標題"), ("conly", "只發評論"), ("nosep", "拿掉分隔線"))


def _review_kb(uid: str, flags: set[str] = frozenset()) -> dict:
    return {"inline_keyboard": [
        [{"text": "✅ 發佈", "callback_data": f"th:pub:{uid}"},
         {"text": "✍️ 加評論", "callback_data": f"th:cmt:{uid}"},
         {"text": "⏭ 略過", "callback_data": f"th:skip:{uid}"}],
        [{"text": "✏️ 改寫全文", "callback_data": f"th:rw:{uid}"}],
        [{"text": ("☑️ " if k in flags else "") + label, "callback_data": f"th:{k}:{uid}"} for k, label in REVIEW_TOGGLES],
    ]}


def _send_review_media(chat_id: str, media: list[dict]) -> int | None:
    """審核預覽先傳貼文的照片／影片（10 個一組），回傳第一則的 message_id。
    整組傳不了就拿掉影片只傳照片；還是不行就放棄（不擋審核）。"""
    first = None
    for i in range(0, len(media), 10):
        group = [dict(m) for m in media[i:i + 10]]
        try:
            sent = _send_media_group(chat_id, group)
        except Exception as e:
            logging.warning("審核預覽的媒體傳不了：%s", e)
            photos = [dict(m) for m in media[i:i + 10] if m["type"] == "photo"]
            if not photos or len(photos) == len(group):
                continue
            try:
                sent = _send_media_group(chat_id, photos)
            except Exception as e2:
                logging.warning("審核預覽只傳照片也失敗：%s", e2)
                continue
        first = first or sent[0]["message_id"]
    return first


def send_review(text: str, uid: str, media: list[dict] | None = None) -> int:
    """media：[{"type": "photo"|"video", "media": 網址}]，先傳相簿，預覽文字＋按鈕回覆在它下面（按鈕不能掛在相簿上）。"""
    chat_id = os.environ["TELEGRAM_ADMIN_CHAT_ID"]
    extra = {}
    if media:
        album_id = _send_review_media(chat_id, media)
        if album_id:
            extra["reply_parameters"] = {"message_id": album_id, "allow_sending_without_reply": True}
        else:
            text = "⚠️ 照片／影片附不上，請到原貼文看\n" + text
    return _call("sendMessage", chat_id=chat_id, text=text[:MESSAGE_LIMIT],
                 reply_markup=_review_kb(uid), disable_web_page_preview=True, **extra)["message_id"]


def set_review_keyboard(message_id: int, uid: str, flags: set[str]) -> None:
    _call("editMessageReplyMarkup", chat_id=os.environ["TELEGRAM_ADMIN_CHAT_ID"], message_id=message_id,
          reply_markup=_review_kb(uid, flags))


def ask_rewrite(draft: str) -> int:
    """草稿放在 <pre>，手機點一下就複製；使用者回覆這則 = 改好的全文。"""
    head = "✏️ 點下面草稿複製，改好後「回覆」這則訊息貼上全文（照你的版本發，超過 500 字自動切段）：\n\n"
    room = MESSAGE_LIMIT - len(head) - 20
    body = html.escape(draft if len(draft) <= room else draft[:room] + "…")
    return _call("sendMessage", chat_id=os.environ["TELEGRAM_ADMIN_CHAT_ID"], text=f"{head}<pre>{body}</pre>",
                 parse_mode="HTML", reply_markup={"force_reply": True, "input_field_placeholder": "改好的全文"})["message_id"]


def split_plain(text: str, limit: int) -> list[str]:
    """改寫全文用：超過 limit 依行切段，第 2 段起加「（續 n/N）」。"""
    if len(text) <= limit:
        return [text]
    cap = limit - 12
    parts, cur = [], ""
    for line in text.split("\n"):
        while len(line) > cap:
            if cur:
                parts.append(cur.rstrip("\n")); cur = ""
            parts.append(line[:cap]); line = line[cap:]
        if len(cur) + len(line) + 1 > cap:
            parts.append(cur.rstrip("\n")); cur = ""
        cur += line + "\n"
    parts.append(cur.rstrip("\n"))
    parts = [p for p in parts if p.strip()]
    n = len(parts)
    return [p if i == 0 else f"（續 {i + 1}/{n}）\n{p}" for i, p in enumerate(parts)]


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


# ───── 翻不出來時挑譯法（choice.py）─────
_RR_INIT = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"]
_RR_MED = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we", "wi", "yu",
           "eu", "ui", "i"]
_RR_FIN = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l", "l", "p", "l", "m", "p", "p", "t", "t",
           "ng", "t", "t", "k", "t", "p", "t"]


def romanize(text: str) -> str:
    """韓文 → 羅馬拼音（Revised Romanization 逐字轉，不處理連音／音變），給使用者自己翻時參考。"""
    out = []
    for ch in text:
        n = ord(ch) - 0xAC00
        if 0 <= n < 11172:
            out.append(_RR_INIT[n // 588] + _RR_MED[n % 588 // 28] + _RR_FIN[n % 28])
        else:
            out.append(ch)
    return "".join(out)


def send_choice(cid: int, src: str, candidates: list[dict], link: str, search_error: str | None = None,
                findings: list[dict] | None = None) -> int:
    """原文＋查到的資料＋候選＋按鈕（候選 N／保留原文／自己打），回 message_id。"""
    lines = ["🔎 這則翻不出來，上網查了候選，選一個就推播（沒選會一直等）", f"出處：{link}", "",
             f"原文：{src[:800]}", f"拼音：{romanize(src[:800])}", ""]
    if findings:
        lines += ["🔍 查到的資料"] + [f"・{f['term'][:40]}：{f['found'][:120]}" for f in findings[:6]] + [""]
    if candidates:
        for i, c in enumerate(candidates, 1):
            lines += [f"{i}. {c['text'][:800]}", f"   根據：{c.get('why') or '（沒寫）'}", ""]
    else:
        lines += [f"查不到候選（{(search_error or '沒有結果')[:200]}），可以保留原文或自己打。", ""]
    text = "\n".join(lines).rstrip()
    if len(text) > MESSAGE_LIMIT:
        text = text[: MESSAGE_LIMIT - 1] + "…"
    kb = [[{"text": f"候選 {i}", "callback_data": f"tc:{i}:{cid}"} for i in range(1, len(candidates) + 1)],
          [{"text": "保留原文", "callback_data": f"tc:keep:{cid}"}, {"text": "自己打", "callback_data": f"tc:own:{cid}"}]]
    return _call("sendMessage", chat_id=os.environ["TELEGRAM_ADMIN_CHAT_ID"], text=text, disable_web_page_preview=True,
                 reply_markup={"inline_keyboard": [r for r in kb if r]})["message_id"]


def send_choice_reminder(message_id: int | None, link: str) -> None:
    extra = {"reply_parameters": {"message_id": message_id, "allow_sending_without_reply": True}} if message_id else {}
    _call("sendMessage", chat_id=os.environ["TELEGRAM_ADMIN_CHAT_ID"], text=f"⏰ 這則還在等你挑譯法：{link}",
          disable_web_page_preview=True, **extra)


def ask_choice_text(src: str) -> int:
    return _call("sendMessage", chat_id=os.environ["TELEGRAM_ADMIN_CHAT_ID"],
                 text=f"回覆這則訊息，輸入要發出去的譯文：\n{src[:800]}\n拼音：{romanize(src[:800])}",
                 reply_markup={"force_reply": True, "input_field_placeholder": "譯文"})["message_id"]


class HardTimeout(RuntimeError):
    pass


@contextmanager
def _hard_limit(sec: int):
    """整個請求的硬上限（SIGALRM，只在主執行緒有效）。
    requests 的 timeout 是每次讀寫各算，2026-11-13 連線被重設後還是卡在讀 TG 更新好幾十分鐘，所以再加這層。
    不繼承 OSError，免得被 urllib3 當成網路錯誤吞掉重試。"""
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGALRM"):
        yield
        return

    def boom(*_):
        raise HardTimeout(f"Telegram 請求超過 {sec} 秒沒回，放棄這次")

    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(sec)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def get_updates(offset: int | None, timeout: int = 0) -> list[dict]:
    url = API.format(token=os.environ["TELEGRAM_BOT_TOKEN"], method="getUpdates")
    payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    with _hard_limit(timeout + 30):
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
    return "\n".join(lines)


def send_video(artist: str, video: dict, zh_title: str | None, hashtags: list[str] | None = None,
               text: str | None = None) -> None:
    text = html.escape(text or format_post(artist, video, zh_title, hashtags))
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    if video.get("thumbnail"):
        try:
            _call("sendPhoto", chat_id=chat_id, photo=video["thumbnail"], caption=text, parse_mode="HTML")
            return
        except RuntimeError:
            pass  # 縮圖失敗就改送純文字
    _call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


def format_live(artist: str, live: dict, zh_title: str | None, hashtags: list[str] | None = None) -> str:
    """Weverse 直播通知（純文字）。只推開播一次；bot 第一次看到時已經結束（直播太短）就改推回放。"""
    member = "（會員限定）" if live.get("membership_only") else ""
    who = artist if live["author"] in ("", artist) else f"{artist} {live['author']}"
    head = f"🔴 {who} 正在 Weverse 直播！{member}" if live.get("live") else f"📺 {who} 的 Weverse 直播已結束，可看回放{member}"
    lines = [head]
    if live["title"]:
        lines.append(f"「{zh_title or live['title']}」")
    lines.append(f"▶ {live['url']}")
    if hashtags:
        lines.append(" ".join(hashtags))
    return "\n".join(lines)


def send_live(artist: str, live: dict, zh_title: str | None, hashtags: list[str] | None = None) -> None:
    """只推 TG 頻道（不發 Threads／FB、不審核）。"""
    send_video(artist, live, zh_title, hashtags, text=format_live(artist, live, zh_title, hashtags))


CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


def _post_parts(artist: str, post: dict, hashtags: list[str] | None, platform: str,
                link: str | None = None) -> tuple[str, list[str]]:
    """標題行、尾巴（含影片提示／hashtag）。預設不放連結（使用者 2026-11-04 決定）；
    只有 IG 推 TG 帶 link（2026-11-09 決定）。"""
    member = "（會員限定）" if post.get("membership_only") else ""
    if post.get("kind") == "notice":
        head = f"{artist} 官方在 {platform} 發佈了公告！📢"
    else:
        head = f"{artist} {post['author']} 在 {platform} 發佈了新貼文！{'📷' if platform == 'Instagram' else '💬'}{member}"
    tail = []
    if post.get("has_video"):
        tail.append(f"（貼文含影片，請到 {platform} 觀看）")
    if hashtags:
        tail.append(" ".join(hashtags))
    if link:
        tail.append(f"🔗 {link}")
    return head, tail


def format_weverse_post(artist: str, post: dict, zh_body: str | None, hashtags: list[str] | None = None,
                        max_len: int = MESSAGE_LIMIT, more: str = "…", platform: str = "Weverse",
                        link: str | None = None) -> str:
    """Weverse／Instagram 成員貼文（純文字）。只放譯文；翻譯失敗或不用翻才用原文。超過 max_len 截內文並接 more。"""
    head, tail = _post_parts(artist, post, hashtags, platform, link)
    body = zh_body or post["body"]

    def build(b: str) -> str:
        blocks = [head] + ([b] if b.strip() else []) + (["\n".join(tail)] if tail else [])
        return "\n\n".join(blocks)

    plain = build(body)
    if len(plain) > max_len:
        body = body[: max(0, len(body) - (len(plain) - max_len) - len(more))] + more
        plain = build(body)
    return plain


def split_threads_post(artist: str, post: dict, zh_body: str | None, hashtags: list[str] | None,
                       platform: str, limit: int) -> list[str]:
    """Threads 版：超過 limit 不截斷，切成多段，第 2 段起當前一段的回覆（留言接續）。
    第 1 段：標題＋內文開頭＋hashtag（主題標籤要在主貼文）；最後一段結尾接含影片提示；中間段只有內文。"""
    full = format_weverse_post(artist, post, zh_body, hashtags, max_len=10**9, platform=platform)
    if len(full) <= limit:
        return [full]
    head, tail_lines = _post_parts(artist, post, hashtags, platform)
    body = zh_body or post["body"]
    tag_line = " ".join(hashtags) if hashtags else ""
    tail = "\n".join(l for l in tail_lines if l != tag_line)
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
        if i == n - 1 and tail:
            s += f"\n\n{tail}"
        out.append(s[:limit])
    return out


def _shrink(raw: bytes, side: int = 2560) -> bytes:
    """壓成長邊 side px 的 JPEG（原圖動輒 5MB 以上，整組上傳會逾時）。壓失敗就傳原檔。"""
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
        im.thumbnail((side, side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return raw


UPLOAD_LIMIT = 50 * 1024 * 1024  # Bot API 自己上傳的上限


def _download(m: dict) -> tuple[str, bytes]:
    """下載一個 media，回傳 (檔名, 內容)。照片壓縮；影片超過上傳上限丟 RuntimeError。"""
    r = requests.get(m["media"], timeout=180)
    r.raise_for_status()
    if m["type"] == "video":
        if len(r.content) > UPLOAD_LIMIT:
            raise RuntimeError(f"影片 {len(r.content) // 1024 // 1024}MB 超過 Telegram 上傳上限")
        return "v.mp4", r.content
    return "p.jpg", _shrink(r.content)


def _send_media_group(chat_id: str, media: list[dict]) -> list[dict]:
    """先讓 Telegram 用網址抓（照片 5MB、影片 20MB 上限）；失敗就自己下載（照片壓縮）再上傳。
    只有一個且是影片時改用 sendVideo（sendMediaGroup 要 2 個以上）。"""
    single_video = len(media) == 1 and media[0]["type"] == "video"
    try:
        if single_video:
            m = media[0]
            extra = {k: m[k] for k in ("caption", "parse_mode") if k in m}
            return [_call("sendVideo", chat_id=chat_id, video=m["media"], supports_streaming=True, **extra)]
        return _call("sendMediaGroup", chat_id=chat_id, media=media)
    except RuntimeError:
        pass
    files, up, total = {}, [], 0
    for i, m in enumerate(media):
        name, raw = _download(m)
        total += len(raw)
        files[f"f{i}"] = (f"f{i}_{name}", raw)
        up.append({**m, "media": f"attach://f{i}"})
    if total > UPLOAD_LIMIT:
        raise RuntimeError(f"整組 {total // 1024 // 1024}MB 超過 Telegram 上傳上限")
    if single_video:
        m = up[0]
        data = {"chat_id": chat_id, "supports_streaming": "true", "video": m["media"],
                **{k: m[k] for k in ("caption", "parse_mode") if k in m}}
        method = "sendVideo"
    else:
        data = {"chat_id": chat_id, "media": json.dumps(up)}
        method = "sendMediaGroup"
    url = API.format(token=os.environ["TELEGRAM_BOT_TOKEN"], method=method)
    res = requests.post(url, data=data, files=files, timeout=300).json()
    if not res.get("ok"):
        raise RuntimeError(f"Telegram {method}（上傳）失敗：{res.get('description')}")
    return [res["result"]] if single_video else res["result"]


def _send_with_media(chat_id: str, media: list[dict], plain: str) -> int:
    """送相簿（文字短就當第一個的 caption，長就另發一則），回傳帶內文那則的 message_id。"""
    text = html.escape(plain)
    if len(plain) <= CAPTION_LIMIT:
        media[0].update(caption=text, parse_mode="HTML")
        return _send_media_group(chat_id, media)[0]["message_id"]
    _send_media_group(chat_id, media)
    return _call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=True)["message_id"]


def send_weverse_post(artist: str, post: dict, zh_body: str | None, hashtags: list[str] | None = None,
                      platform: str = "Weverse") -> int:
    """回傳帶內文那則訊息的 message_id（之後成員留言用回覆掛在它下面）。
    IG：直接傳影片＋照片並附連結；影片傳不了就退回縮圖＋「請到 IG 觀看」。"""
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    link = post.get("url") if platform == "Instagram" else None
    items = post.get("media", [])[:10]
    if platform == "Instagram" and any(m["type"] == "video" for m in items):
        plain = format_weverse_post(artist, {**post, "has_video": False}, zh_body, hashtags, platform=platform, link=link)
        try:
            return _send_with_media(chat_id, [{"type": m["type"], "media": m["url"]} for m in items], plain)
        except Exception as e:
            logging.warning("IG 影片傳 TG 失敗，改傳縮圖：%s", e)
    photos = post.get("photos", [])[:10]
    plain = format_weverse_post(artist, post, zh_body, hashtags, platform=platform, link=link)
    if not photos:
        return _call("sendMessage", chat_id=chat_id, text=html.escape(plain), parse_mode="HTML",
                     disable_web_page_preview=True)["message_id"]
    return _send_with_media(chat_id, [{"type": "photo", "media": u} for u in photos], plain)


def _clip(text: str, n: int) -> str:
    """截到 n 字以內，截到的話尾巴放「…」。"""
    if len(text) <= n:
        return text
    return text[: n - 1] + "…" if n > 0 else ""


def format_weverse_comment(comments: list[dict], zh_bodies: list[str | None],
                           zh_reply_to: str | None = None, limit: int | None = None, quote: bool = True) -> str:
    """粉絲（或成員名）：被回覆的留言譯文 換行 成員：回覆譯文；沒回覆別人就只有 成員：譯文。
    不加標題、「回覆」字樣、連結、AI 註記（使用者 2026-11-04 決定）。

    comments 是同一位成員對同一則留言的回覆（同一輪合併，依時間排序）；多則時被回覆那句只出現一次，
    成員每句一行。zh 為 None（不用翻，例如只有 emoji）就用原文。
    limit（Threads 500 字）：超過時先縮被回覆的那句，還不夠從最早的回覆開始縮。
    """
    author = comments[0]["author"]
    rt = comments[0].get("reply_to") if quote else None  # quote=False：接在被回覆留言的推播底下，不重複引用
    bodies = [zh or c["body"] for c, zh in zip(comments, zh_bodies)]
    quoted = (zh_reply_to or rt["body"]) if rt else ""

    def build(q: str, bs: list[str]) -> str:
        lines = [f"{rt['author'] or '粉絲'}：{q}"] if rt else []
        return "\n".join(lines + [f"{author}：{b}" for b in bs])

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


def format_comment_digest(items: list[dict], limit: int) -> list[dict]:
    """成員留言彙整（同一篇貼文一段時間內的全部成員留言，依時間排序）→ 一則或多則純文字（每則不超過 limit）。

    items 每筆：{"comment": weverse 留言, "zh": 譯文, "zh_reply_to": 被回覆那句譯文, "zh_nickname": 粉絲暱稱譯文或 None}。
    分兩則（使用者 2026-11-26 決定）：「成員 留言（n 則）」（自己直接留言、回成員）和「成員 回粉絲（n 則）」各自一則，每段用「・」條列。
    回傳 [{"key": "own"／"fan", "uids": 這則含的留言 uid, "parts": 文字（超過 limit 才切多段，標題加（i/N））}]，沒內容的那則不回。
    回覆別人的段落：「・粉絲（或成員名）：被回覆譯文」下面接「　↳ 回覆譯文」；同一位成員連回同一則同一段、被回覆那句只出現一次；
    回覆同一份彙整裡的留言 → 接在那段底下。只有一位成員時不重複寫成員名（標題已有）。
    """
    authors = list(dict.fromkeys(it["comment"]["author"] for it in items))
    solo = len(authors) == 1
    blocks: list[dict] = []  # {"fan": bool, "head": str|None, "lines": [str]}
    where: dict = {}  # 留言 uid／(作者, 被回覆 id) → 段落 index
    for it in items:
        c = it["comment"]
        rt = c.get("reply_to")
        body = it.get("zh") or c["body"]
        said = body if solo else f"{c['author']}：{body}"
        parent = f"wvc:{rt['id']}" if rt and rt.get("id") else None
        same = (c["author"], rt["id"]) if parent else None
        i = where.get(parent) if parent in where else where.get(same)
        if i is None:
            if rt:
                who = rt.get("author") or (f"粉絲（{it['zh_nickname']}）" if it.get("zh_nickname") else "粉絲")
                blocks.append({"fan": not rt.get("author"), "head": f"{who}：{it.get('zh_reply_to') or rt['body']}", "lines": []})
            else:
                blocks.append({"fan": False, "head": None, "lines": []})
            i = len(blocks) - 1
        blocks[i]["lines"].append(said)
        where[c["uid"]] = i
        if same:
            where.setdefault(same, i)

    def render(bk: dict) -> str:
        if bk["head"]:
            first, rest = bk["head"], bk["lines"]
        else:
            first, rest = bk["lines"][0], bk["lines"][1:]
        # 多行留言（例如選項 1) 2) 3)）第二行起縮排對齊，不跟條列混在一起
        return "\n".join([f"・{first}".replace("\n", "\n　")] + [f"　↳ {x}".replace("\n", "\n　　") for x in rest])

    out = []
    for key, fan in (("own", False), ("fan", True)):
        bks = [b for b in blocks if b["fan"] == fan]
        if not bks:
            continue
        uids = [it["comment"]["uid"] for it in items if blocks[where[it["comment"]["uid"]]]["fan"] == fan]
        who = list(dict.fromkeys(it["comment"]["author"] for it in items if it["comment"]["uid"] in uids))
        title = f"{'、'.join(who)} {'回粉絲' if fan else '留言'}" + (f"（{len(uids)} 則）" if len(uids) > 1 else "")
        cap = limit - len(title) - 12  # 12 給「（i/N）」和換行
        parts: list[str] = []
        sep = "\n\n" if fan else "\n"  # 回粉絲每組對話之間空一行（使用者 2026-11-26）
        for bk in bks:
            txt = _clip(render(bk), cap)
            if parts and len(parts[-1]) + len(sep) + len(txt) <= cap:
                parts[-1] += sep + txt
            else:
                parts.append(txt)
        n = len(parts)
        out.append({"key": key, "uids": uids,
                    "parts": [f"{title}{f'（{i}/{n}）' if n > 1 else ''}\n\n{p}" for i, p in enumerate(parts, start=1)]})
    return out


def send_weverse_comment(text: str, reply_to_message_id: int | None) -> int:
    """text 是 format_weverse_comment 的純文字。"""
    text = html.escape(text)
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    extra = {}
    if reply_to_message_id:
        extra["reply_parameters"] = {"message_id": reply_to_message_id, "allow_sending_without_reply": True}
    return _call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                 disable_web_page_preview=True, **extra)["message_id"]
