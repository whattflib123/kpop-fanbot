"""Threads 發文前先問使用者（TG 個人聊天室 TELEGRAM_ADMIN_CHAT_ID）。

流程：
1. 新貼文排進 threads_queue 後，私訊預覽＋按鈕「發佈／加評論／略過」（threads_review 記 asked）。
2. 使用者按鈕 → callback：
   - 發佈：approved，下輪 flush 直接發。
   - 加評論：await_comment，請使用者回一段文字；收到後把評論放在貼文最前面，轉 approved。
   - 略過：skipped，從佇列刪掉這篇和它的續段；之後這篇底下的成員留言也不發。
   - ✏️ 改寫：bot 傳整篇草稿（點一下可複製），使用者回覆改好的全文 → 照這版發（超過 500 字自動切段接回覆），轉 approved。
   - 快速切換（可多選，按了按鈕前面打勾）：去標題（拿掉第一行「XX 發佈了新貼文」）、只發評論（不附譯文和續段，
     要再按加評論）、拿掉分隔線（評論和譯文之間不放「以下為翻譯」）。切換只決定格式，最後還是要按發佈或加評論。
3. THREADS_REVIEW_HOURS（預設 24）小時沒回 → 自動發佈。
只審貼文（第 1 段）；續段跟著第 1 段，成員留言回覆不審。

TG 更新用 getUpdates 長輪詢，在兩輪輪詢之間跑（main.wait）。同一個 bot token 不能同時有別的程式在 getUpdates，
不然會 409；本機要抓私訊內容請先停 NAS 容器。
"""
import json
import logging
import os
import sqlite3
import time

import notify
import notify_threads

STATES = ("asked", "await_comment", "await_rewrite", "approved", "skipped")
PENDING = ("asked", "await_comment", "await_rewrite")  # 還在等使用者
SEPARATOR = "——— 以下為翻譯 ———"  # 使用者評論和譯文之間
FLAGS = {"nohead": "去標題", "conly": "只發評論", "nosep": "拿掉分隔線"}


def with_comment(comment: str, text: str) -> str:
    return f"{comment}\n\n{SEPARATOR}\n\n{text}"


def compose(text: str, comment: str | None, flags: set[str]) -> str:
    """第 1 段的最終內容。text 是原本第 1 段（標題行＋空行＋內文…）。"""
    if "nohead" in flags and "\n\n" in text:
        text = text.split("\n\n", 1)[1]
    if not comment:
        return text
    if "conly" in flags:
        return comment
    return f"{comment}\n\n{text}" if "nosep" in flags else with_comment(comment, text)


def _flags(db: sqlite3.Connection, uid: str) -> set[str]:
    row = db.execute("SELECT flags FROM threads_review WHERE uid=?", (uid,)).fetchone()
    return set(json.loads(row[0])) if row and row[0] else set()


def final(db: sqlite3.Connection, uid: str, text: str) -> tuple[str, list[str] | None]:
    """flush 用：回 (第 1 段要發的內容, 續段)。續段 None = 照原本佇列裡的續段；list = 換成這些（[] = 不發續段）。"""
    row = db.execute("SELECT comment, override FROM threads_review WHERE uid=?", (uid,)).fetchone()
    if not row:
        return text, None
    comment, override = row
    if override:
        parts = notify.split_plain(override, notify_threads.TEXT_LIMIT)
        return parts[0], parts[1:]
    flags = _flags(db, uid)
    out = compose(text, comment, flags)
    return out, ([] if comment and "conly" in flags else None)


def enabled() -> bool:
    # Threads 沒開就不審、也不長輪詢 TG（h2h-fanbot 先修，2026-11-03 帶回）
    return (notify_threads.enabled() and bool(os.environ.get("TELEGRAM_ADMIN_CHAT_ID"))
            and os.environ.get("THREADS_REVIEW", "1") != "0")


def timeout_sec() -> int:
    return int(float(os.environ.get("THREADS_REVIEW_HOURS", "24")) * 3600)


def init_db(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS threads_review (uid TEXT PRIMARY KEY, message_id INTEGER, prompt_id INTEGER,"
               " status TEXT, comment TEXT, created INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    cols = {r[1] for r in db.execute("PRAGMA table_info(threads_review)")}
    for col in ("override", "flags"):  # 2026-11-06 加：改寫全文、快速切換
        if col not in cols:
            db.execute(f"ALTER TABLE threads_review ADD COLUMN {col} TEXT")


def ask(db: sqlite3.Connection, uid: str, text: str, n_parts: int, n_photos: int, limit: int) -> None:
    """私訊預覽並記 asked。失敗就不記，flush 會視為不需審核直接發（寧可發也不要卡住）。"""
    if not enabled():
        return
    notes = []
    if n_parts > 1:
        notes.append(f"分 {n_parts} 段，後段接在留言")
    if n_photos:
        notes.append(f"附 {n_photos} 張圖")
    room = limit - len(text) - len(SEPARATOR) - 4
    head = "📝 Threads 待審" + (f"（{'，'.join(notes)}）" if notes else "") + f"\n評論最多 {max(room, 0)} 字\n\n"
    try:
        mid = notify.send_review(head + text, uid)
    except Exception as e:
        logging.warning("送審核預覽失敗 %s，改為直接發：%s", uid, e)
        return
    db.execute("INSERT OR REPLACE INTO threads_review (uid, message_id, prompt_id, status, comment, created)"
               " VALUES (?,?,?,?,?,?)", (uid, mid, None, "asked", None, int(time.time())))
    db.commit()


def decision(db: sqlite3.Connection, uid: str) -> tuple[str, str | None]:
    """flush 用：回 (狀態, 評論)。沒審核紀錄＝approved；逾時自動 approved。"""
    row = db.execute("SELECT status, comment, created, message_id FROM threads_review WHERE uid=?", (uid,)).fetchone()
    if not row:
        return "approved", None
    status, comment, created, mid = row
    if status in PENDING and time.time() - created >= timeout_sec():
        db.execute("UPDATE threads_review SET status='approved' WHERE uid=?", (uid,))
        db.commit()
        _edit(mid, f"⏰ {os.environ.get('THREADS_REVIEW_HOURS', '24')} 小時未回，已自動發佈")
        return "approved", None
    return status, comment


def mark_posted(db: sqlite3.Connection, uid: str, media_id: str) -> None:
    row = db.execute("SELECT message_id FROM threads_review WHERE uid=?", (uid,)).fetchone()
    if row:
        _edit(row[0], f"✅ 已發到 Threads（{media_id}）")
        db.execute("DELETE FROM threads_review WHERE uid=?", (uid,))
        db.commit()


def _edit(message_id: int | None, status_line: str) -> None:
    if not message_id:
        return
    try:
        notify.edit_review(message_id, status_line)
    except Exception as e:
        logging.warning("更新審核訊息失敗：%s", e)


def wait(db: sqlite3.Connection, seconds: float) -> None:
    """兩輪輪詢之間：長輪詢 TG 更新，處理按鈕和評論回覆。review 關閉時就單純 sleep。"""
    deadline = time.time() + seconds
    if not enabled():
        time.sleep(max(0, seconds))
        return
    while True:
        remain = deadline - time.time()
        if remain <= 0:
            return
        try:
            handle_updates(db, timeout=int(min(remain, 50)))
        except Exception as e:
            logging.warning("讀 TG 更新失敗：%s", e)
            time.sleep(min(remain, 15))


def handle_updates(db: sqlite3.Connection, timeout: int = 0) -> None:
    row = db.execute("SELECT v FROM kv WHERE k='tg_offset'").fetchone()
    offset = int(row[0]) if row else None
    updates = notify.get_updates(offset, timeout)
    admin = os.environ["TELEGRAM_ADMIN_CHAT_ID"]
    for u in updates:
        db.execute("INSERT OR REPLACE INTO kv VALUES ('tg_offset', ?)", (str(u["update_id"] + 1),))
        try:
            if "callback_query" in u:
                _on_callback(db, u["callback_query"], admin)
            elif "message" in u and str(u["message"].get("chat", {}).get("id")) == str(admin):
                _on_message(db, u["message"])
        except Exception as e:
            logging.warning("處理 TG 更新失敗：%s", e)
        db.commit()


def _on_callback(db: sqlite3.Connection, cq: dict, admin: str) -> None:
    data = cq.get("data") or ""
    if not data.startswith("th:") or str(cq.get("from", {}).get("id")) != str(admin):
        notify.answer_callback(cq["id"], "不是你的按鈕")
        return
    _, action, uid = data.split(":", 2)
    row = db.execute("SELECT status, message_id FROM threads_review WHERE uid=?", (uid,)).fetchone()
    if not row or row[0] in ("approved", "skipped"):
        notify.answer_callback(cq["id"], "這篇已處理過")
        return
    status, mid = row
    if action in FLAGS:
        flags = _flags(db, uid) ^ {action}
        db.execute("UPDATE threads_review SET flags=? WHERE uid=?", (json.dumps(sorted(flags)), uid))
        notify.set_review_keyboard(mid, uid, flags)
        notify.answer_callback(cq["id"], f"{FLAGS[action]}：{'開' if action in flags else '關'}")
        return
    if action == "pub" and "conly" in _flags(db, uid):
        notify.answer_callback(cq["id"], "「只發評論」要先按加評論")
        return
    if action == "pub":
        db.execute("UPDATE threads_review SET status='approved' WHERE uid=?", (uid,))
        notify.answer_callback(cq["id"], "下一輪發佈")
        _edit(mid, "✅ 已核准，下一輪發佈")
    elif action == "skip":
        db.execute("UPDATE threads_review SET status='skipped' WHERE uid=?", (uid,))
        db.execute("DELETE FROM threads_queue WHERE uid=? OR uid LIKE ?", (uid, f"{uid}#%"))
        notify.answer_callback(cq["id"], "已略過")
        _edit(mid, "⏭ 已略過，不發 Threads")
    elif action == "cmt":
        pid = notify.ask_comment(f"回覆這則訊息，輸入要放在貼文最前面的評論（{uid}）")
        db.execute("UPDATE threads_review SET status='await_comment', prompt_id=? WHERE uid=?", (pid, uid))
        notify.answer_callback(cq["id"], "請回覆評論")
    elif action == "rw":
        rows = db.execute("SELECT uid, payload FROM threads_queue WHERE uid=? OR uid LIKE ?", (uid, f"{uid}#%")).fetchall()
        rows.sort(key=lambda r: int(r[0].rsplit("#", 1)[1]) if "#" in r[0] else 1)  # 第 1 段、#2、#3…（#10 排在 #9 後）
        parts = [r[1] for r in rows]
        if not parts:
            notify.answer_callback(cq["id"], "這篇已不在佇列")
            return
        texts = [json.loads(p)["text"] for p in parts]
        texts[0] = compose(texts[0], None, _flags(db, uid))  # 去標題有開就給沒標題的草稿
        draft = "\n".join([texts[0]] + [t.split("\n", 1)[1] if t.startswith("（續 ") else t for t in texts[1:]])
        pid = notify.ask_rewrite(draft)
        db.execute("UPDATE threads_review SET status='await_rewrite', prompt_id=? WHERE uid=?", (pid, uid))
        notify.answer_callback(cq["id"], "複製草稿改好後回覆")
    logging.info("Threads 審核 %s → %s", uid, action)


def _on_message(db: sqlite3.Connection, msg: dict) -> None:
    text = (msg.get("text") or "").strip()
    if not text or text.startswith("/"):
        return
    reply_id = (msg.get("reply_to_message") or {}).get("message_id")
    row = None
    if reply_id:
        row = db.execute("SELECT uid, message_id, status FROM threads_review WHERE status IN ('await_comment','await_rewrite')"
                         " AND prompt_id=?", (reply_id,)).fetchone()
    if not row:  # 沒用回覆：拿最近一則等評論／改寫的
        row = db.execute("SELECT uid, message_id, status FROM threads_review WHERE status IN ('await_comment','await_rewrite')"
                         " ORDER BY created DESC").fetchone()
    if not row:
        return
    uid, mid, status = row
    q = db.execute("SELECT payload FROM threads_queue WHERE uid=?", (uid,)).fetchone()
    if not q:
        db.execute("DELETE FROM threads_review WHERE uid=?", (uid,))
        notify.send_admin("這篇已不在佇列，評論沒有加上")
        return
    if status == "await_rewrite":
        db.execute("UPDATE threads_review SET status='approved', override=? WHERE uid=?", (text, uid))
        n = len(notify.split_plain(text, notify_threads.TEXT_LIMIT))
        _edit(mid, f"✅ 已改寫，下一輪發佈" + (f"（分 {n} 段）" if n > 1 else ""))
        logging.info("Threads 審核 %s 改寫 %d 字", uid, len(text))
        return
    body = json.loads(q[0])["text"]
    flags = _flags(db, uid)
    over = len(compose(body, text, flags)) - notify_threads.TEXT_LIMIT
    if over > 0:
        notify.send_admin(f"評論太長（{len(text)} 字），最多 {len(text) - over} 字，請再回一次")
        return
    db.execute("UPDATE threads_review SET status='approved', comment=? WHERE uid=?", (text, uid))
    _edit(mid, f"✅ 已加評論，下一輪發佈：\n{text}")
    logging.info("Threads 審核 %s 加評論 %d 字", uid, len(text))
