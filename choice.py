"""翻不出來的內容讓使用者挑譯法（TG 個人聊天室 TELEGRAM_ADMIN_CHAT_ID）。

流程：
1. translate 丟 NeedsChoice（韓文原樣吐回兩次 → 已上網查候選）→ ask() 私訊原文＋候選＋按鈕，那則內容先不推。
2. 使用者按「候選 N」或「保留原文」，或按「自己打」再回覆譯文 → 存進 translate_choice（status=done），
   叫醒主迴圈，下一輪 translate 直接回傳這個譯文，照正常流程推 TG／排 Threads 審核。
3. 挑候選時，候選裡的關鍵詞記進 glossary_learned，之後翻譯併進詞彙表，同一個詞不會再問。
4. 沒挑就一直等（不算翻譯失敗、不會放棄），每 CHOICE_REMIND_HOURS（預設 24）小時提醒一次。
同一段原文（例如同一句出現在好幾則）只問一次。
"""
import json
import logging
import os
import sqlite3
import time

import notify
import translate

_wake = False


def init_db(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS translate_choice (id INTEGER PRIMARY KEY AUTOINCREMENT, src TEXT UNIQUE,"
               " uid TEXT, url TEXT, candidates TEXT, message_id INTEGER, prompt_id INTEGER, status TEXT,"
               " result TEXT, created INTEGER, reminded INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS glossary_learned (term TEXT PRIMARY KEY, zh TEXT, src_id INTEGER, created INTEGER)")


def load(db: sqlite3.Connection) -> None:
    """每輪開始：把挑好的譯文、等待中的原文、學到的詞交給 translate。"""
    rows = db.execute("SELECT src, status, result FROM translate_choice").fetchall()
    translate.OVERRIDES = {src: result for src, status, result in rows if status == "done"}
    translate.PENDING = {src for src, status, _ in rows if status != "done"}
    translate.LEARNED = dict(db.execute("SELECT term, zh FROM glossary_learned").fetchall())


def pending(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM translate_choice WHERE status!='done' LIMIT 1").fetchone() is not None


def take_wake() -> bool:
    """使用者剛挑完：主迴圈不等滿 POLL_SECONDS，馬上跑下一輪推出去。"""
    global _wake
    w, _wake = _wake, False
    return w


def _remind_sec() -> int:
    return int(float(os.environ.get("CHOICE_REMIND_HOURS", "24")) * 3600)


def ask(db: sqlite3.Connection, err: "translate.NeedsChoice", item: dict) -> None:
    """第一次：私訊候選＋按鈕。之後（asked）：超過提醒間隔就回覆原訊息提醒一次。"""
    row = db.execute("SELECT id, message_id, status, reminded FROM translate_choice WHERE src=?", (err.src,)).fetchone()
    now = int(time.time())
    if row:
        cid, mid, status, reminded = row
        if status != "done" and now - (reminded or 0) >= _remind_sec():
            try:
                notify.send_choice_reminder(mid, item.get("url") or item["uid"])
            except Exception as e:
                logging.warning("提醒挑譯法失敗：%s", e)
                return
            db.execute("UPDATE translate_choice SET reminded=? WHERE id=?", (now, cid))
            db.commit()
        return
    cur = db.execute("INSERT INTO translate_choice (src, uid, url, candidates, status, created, reminded)"
                     " VALUES (?,?,?,?,?,?,?)", (err.src, item["uid"], item.get("url"),
                                                 json.dumps(err.candidates, ensure_ascii=False), "asked", now, now))
    cid = cur.lastrowid
    try:
        mid = notify.send_choice(cid, err.src, err.candidates, item.get("url") or item["uid"], err.search_error,
                                 err.findings)
    except Exception as e:
        logging.warning("私訊挑譯法失敗，下輪再試：%s", e)
        db.execute("DELETE FROM translate_choice WHERE id=?", (cid,))
        db.commit()
        return
    db.execute("UPDATE translate_choice SET message_id=? WHERE id=?", (mid, cid))
    db.commit()
    translate.PENDING.add(err.src)  # 同一輪同一句不再查
    logging.info("翻不出來，私訊挑譯法 #%d %s：%r（%d 個候選）", cid, item["uid"], err.src[:80], len(err.candidates))


def _finish(db: sqlite3.Connection, cid: int, result: str, terms: list[dict], how: str) -> None:
    global _wake
    db.execute("UPDATE translate_choice SET status='done', result=? WHERE id=?", (result, cid))
    now = int(time.time())
    for t in terms:
        db.execute("INSERT OR REPLACE INTO glossary_learned VALUES (?,?,?,?)", (t["term"], t["zh"], cid, now))
    db.commit()
    _wake = True
    mid = db.execute("SELECT message_id FROM translate_choice WHERE id=?", (cid,)).fetchone()[0]
    learned = "；".join(f"{t['term']} → {t['zh']}" for t in terms)
    try:
        notify.edit_review(mid, f"✅ {how}，馬上推播：\n{result}" + (f"\n\n記進詞彙表：{learned}" if learned else ""))
    except Exception as e:
        logging.warning("更新挑譯法訊息失敗：%s", e)
    logging.info("挑譯法 #%d → %s：%r", cid, how, result[:80])


def on_callback(db: sqlite3.Connection, cq: dict) -> None:
    """callback_data = tc:<動作>:<id>；動作是候選序號 1～3、keep（保留原文）、own（自己打）。"""
    _, action, cid = (cq.get("data") or "").split(":", 2)
    row = db.execute("SELECT src, candidates, status FROM translate_choice WHERE id=?", (int(cid),)).fetchone()
    if not row or row[2] == "done":
        notify.answer_callback(cq["id"], "這則已處理過")
        return
    src, cands, _ = row
    cands = json.loads(cands or "[]")
    if action == "keep":
        notify.answer_callback(cq["id"], "保留原文")
        _finish(db, int(cid), src, [], "保留原文")
    elif action == "own":
        pid = notify.ask_choice_text(src)
        db.execute("UPDATE translate_choice SET status='await_text', prompt_id=? WHERE id=?", (pid, int(cid)))
        db.commit()
        notify.answer_callback(cq["id"], "請回覆譯文")
    elif action.isdigit() and 1 <= int(action) <= len(cands):
        c = cands[int(action) - 1]
        notify.answer_callback(cq["id"], f"用候選 {action}")
        _finish(db, int(cid), c["text"], c.get("terms") or [], f"用候選 {action}")
    else:
        notify.answer_callback(cq["id"], "沒有這個選項")


def on_message(db: sqlite3.Connection, msg: dict) -> bool:
    """使用者回覆「自己打」的提示 → 用這段當譯文。回 True 表示這則訊息是給這裡的。"""
    text = (msg.get("text") or "").strip()
    reply_id = (msg.get("reply_to_message") or {}).get("message_id")
    if not text or text.startswith("/") or not reply_id:
        return False
    row = db.execute("SELECT id FROM translate_choice WHERE status='await_text' AND prompt_id=?", (reply_id,)).fetchone()
    if not row:
        return False
    _finish(db, row[0], text, [], "用你打的譯文")
    return True
