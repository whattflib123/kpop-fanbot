"""追星機器人：輪詢來源 → 新影片翻譯 → 推 Telegram。

用法：
  python main.py            # 常駐輪詢
  python main.py --once     # 跑一輪就結束
  python main.py --dry-run  # 不推 TG、不寫 DB，只印出會推什麼
"""
import argparse
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import notify_threads
from sources import instagram, weverse, youtube

ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("DB_PATH", ROOT / "data" / "state.db"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("CREATE TABLE IF NOT EXISTS seen (uid TEXT PRIMARY KEY, source TEXT, title TEXT, zh_title TEXT, ts INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS sources (source TEXT PRIMARY KEY, initialized INTEGER)")
    # 推過的 Weverse 貼文 → TG message_id，成員留言回覆掛在它下面
    db.execute("CREATE TABLE IF NOT EXISTS tg_messages (uid TEXT PRIMARY KEY, message_id INTEGER)")
    # Threads：TG 推成功後排進佇列，失敗不影響 TG，下輪重試
    db.execute("CREATE TABLE IF NOT EXISTS threads_queue (uid TEXT PRIMARY KEY, payload TEXT, reply_to_uid TEXT, attempts INTEGER, created INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS threads_posted (uid TEXT PRIMARY KEY, media_id TEXT)")
    # 翻譯失敗次數；到上限就放棄那則（記 seen、不推）
    db.execute("CREATE TABLE IF NOT EXISTS translate_fail (uid TEXT PRIMARY KEY, attempts INTEGER)")
    return db


THREADS_MAX_ATTEMPTS = 5


def enqueue_threads(db: sqlite3.Connection, uid: str, text: str, images: list[str] | None = None,
                    reply_to_uid: str | None = None) -> None:
    if not notify_threads.enabled():
        return
    payload = json.dumps({"text": text, "images": images or []}, ensure_ascii=False)
    db.execute("INSERT OR IGNORE INTO threads_queue VALUES (?,?,?,0,?)", (uid, payload, reply_to_uid, int(time.time())))


def flush_threads(db: sqlite3.Connection) -> None:
    if not notify_threads.enabled():
        return
    rows = db.execute("SELECT uid, payload, reply_to_uid, attempts FROM threads_queue ORDER BY created").fetchall()
    for uid, payload, reply_to_uid, attempts in rows:
        reply_media = None
        if reply_to_uid:
            if db.execute("SELECT 1 FROM threads_queue WHERE uid=?", (reply_to_uid,)).fetchone():
                continue  # 原貼文還沒發出去，等它
            row = db.execute("SELECT media_id FROM threads_posted WHERE uid=?", (reply_to_uid,)).fetchone()
            reply_media = row[0] if row else None
        p = json.loads(payload)
        try:
            media_id = notify_threads.publish(p["text"], p["images"], reply_media)
        except Exception as e:
            attempts += 1
            if attempts >= THREADS_MAX_ATTEMPTS:
                logging.warning("Threads 發文放棄 %s（%d 次失敗）：%s", uid, attempts, notify_threads._mask(e))
                db.execute("DELETE FROM threads_queue WHERE uid=?", (uid,))
            else:
                logging.warning("Threads 發文失敗 %s，下輪重試：%s", uid, notify_threads._mask(e))
                db.execute("UPDATE threads_queue SET attempts=? WHERE uid=?", (attempts, uid))
            db.commit()
            continue
        logging.info("Threads 已發 %s → %s", uid, media_id)
        db.execute("INSERT OR REPLACE INTO threads_posted VALUES (?,?)", (uid, media_id))
        db.execute("DELETE FROM threads_queue WHERE uid=?", (uid,))
        db.commit()


COMMENT_WINDOW_DAYS = 7
_ig_last_poll = 0.0  # IG 輪詢比其他來源慢（IG_POLL_SECONDS），避免吃光 Meta 的限流額度


def bot_name() -> str:
    return os.environ.get("BOT_NAME", "fanbot")  # 私訊警示的開頭


class TranslateAlert:
    """翻譯失敗時暫停推播（不記 seen，下輪重試），並私訊使用者：開始失敗發一次；之後整輪都沒失敗才發恢復。"""

    def __init__(self) -> None:
        self.failing = False      # 目前處於失敗狀態（已私訊過）
        self.round_failed = False  # 這一輪有沒有翻譯失敗
        self.round_ok = False      # 這一輪有沒有翻譯成功（沒東西可翻不算恢復）
        self.dry_run = False

    def start_round(self, dry_run: bool = False) -> None:
        self.round_failed = self.round_ok = False
        self.dry_run = dry_run

    def fail(self, db: sqlite3.Connection, source: str, item: dict, err: Exception) -> None:
        """記一次失敗；到 TRANSLATE_MAX_ATTEMPTS 次就放棄（記 seen、不推）並私訊。"""
        uid = item["uid"]
        self.round_failed = True
        if self.dry_run:
            logging.warning("翻譯失敗 %s：%s", uid, err)
            return
        max_attempts = int(os.environ.get("TRANSLATE_MAX_ATTEMPTS", "12"))
        row = db.execute("SELECT attempts FROM translate_fail WHERE uid=?", (uid,)).fetchone()
        attempts = (row[0] if row else 0) + 1
        if attempts >= max_attempts:
            logging.warning("翻譯失敗 %d 次，放棄 %s：%s", attempts, uid, err)
            db.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?,?,?)",
                       (uid, source, item.get("title", "")[:80], None, int(time.time())))
            db.execute("DELETE FROM translate_fail WHERE uid=?", (uid,))
            db.commit()
            self._send(f"❌ {bot_name()} 翻譯失敗 {attempts} 次，放棄這則（不推播）：\n{item.get('url', uid)}")
            return
        db.execute("INSERT OR REPLACE INTO translate_fail VALUES (?,?)", (uid, attempts))
        db.commit()
        logging.warning("翻譯失敗（第 %d 次），%s 暫不推播、下輪重試：%s", attempts, uid, err)
        if not self.failing:
            self.failing = True
            self._send(f"⚠️ {bot_name()} 翻譯失敗，新內容暫停推播，每 {os.environ.get('POLL_SECONDS', '300')} 秒重試，"
                       f"同一則最多 {max_attempts} 次。\n原因：{str(err)[:300]}\n恢復後會自動補推。")

    def clear(self, db: sqlite3.Connection, uid: str) -> None:
        self.round_ok = True
        db.execute("DELETE FROM translate_fail WHERE uid=?", (uid,))

    def end_round(self) -> None:
        if self.failing and self.round_ok and not self.round_failed:
            self.failing = False
            self._send(f"✅ {bot_name()} 翻譯恢復，暫停中的內容已補推（已放棄的不會補）。")

    @staticmethod
    def _send(text: str) -> None:
        from notify import send_admin
        try:
            send_admin(text)
        except Exception as e:
            logging.warning("私訊警示失敗：%s", e)


ALERT = TranslateAlert()


def check_comments(db: sqlite3.Connection, artist: dict, community: dict, posts: list[dict], dry_run: bool) -> None:
    """最近 COMMENT_WINDOW_DAYS 天內的成員貼文，檢查底下有沒有新的成員留言。"""
    from notify import format_weverse_comment, send_weverse_comment
    from translate import TranslateError, translate

    source = f"wvc:{community['id']}"
    cutoff = time.time() - COMMENT_WINDOW_DAYS * 86400
    recent = [p for p in posts if datetime.fromisoformat(p["published"]).timestamp() >= cutoff]
    comments = []
    for p in recent:
        try:
            comments += [(p, c) for c in weverse.fetch_artist_comments(p["uid"][4:])]
        except Exception as e:
            logging.warning("抓留言失敗 %s：%s", p["uid"], e)

    if not db.execute("SELECT 1 FROM sources WHERE source=?", (source,)).fetchone():
        if not dry_run:
            db.executemany("INSERT OR IGNORE INTO seen VALUES (?,?,?,?,?)",
                           [(c["uid"], source, c["body"][:80], None, int(time.time())) for _, c in comments])
            db.execute("INSERT INTO sources VALUES (?,1)", (source,))
            db.commit()
        logging.info("初始化 %s 留言：記錄 %d 則既有留言，不推播", community["label"], len(comments))
        return

    show_reply_to = bool(community.get("show_reply_to"))
    new = [(p, c) for p, c in comments if not db.execute("SELECT 1 FROM seen WHERE uid=?", (c["uid"],)).fetchone()]
    for p, c in sorted(new, key=lambda x: x[1]["created"]):
        try:
            zh = translate(c["body"], post=True)
            zh_rt = translate(c["reply_to"]["body"], post=True) if show_reply_to and c["reply_to"] else None
        except TranslateError as e:
            ALERT.fail(db, source, {"uid": c["uid"], "title": c["body"], "url": p["url"]}, e)
            continue
        logging.info("新留言 %s | %s → %s", c["author"], c["body"], zh)
        if dry_run:
            continue
        row = db.execute("SELECT message_id FROM tg_messages WHERE uid=?", (p["uid"],)).fetchone()
        text = format_weverse_comment(c, zh, p["url"], zh_rt, show_reply_to)
        try:
            send_weverse_comment(text, row[0] if row else None)
        except Exception as e:
            logging.warning("留言推播失敗，下輪重試：%s", e)
            continue
        th_text = format_weverse_comment(c, zh, p["url"], zh_rt, show_reply_to, limit=notify_threads.TEXT_LIMIT)
        enqueue_threads(db, c["uid"], th_text, reply_to_uid=p["uid"])
        db.execute("INSERT INTO seen VALUES (?,?,?,?,?)", (c["uid"], source, c["body"][:80], zh, int(time.time())))
        ALERT.clear(db, c["uid"])
        db.commit()


def run_once(db: sqlite3.Connection, dry_run: bool) -> None:
    from notify import format_post, format_weverse_post, send_video, send_weverse_post
    from translate import TranslateError, translate

    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    ALERT.start_round(dry_run)

    def is_seen(uid: str) -> bool:
        return db.execute("SELECT 1 FROM seen WHERE uid=?", (uid,)).fetchone() is not None

    global _ig_last_poll
    ig_due = bool(os.environ.get("META_PAGE_TOKEN")) and \
        time.time() - _ig_last_poll >= int(os.environ.get("IG_POLL_SECONDS", "900")) - 30
    if ig_due:
        _ig_last_poll = time.time()

    for artist in config["artists"]:
        feeds = [
            (f"yt:{ch['id']}", ch["label"], lambda ch=ch: youtube.fetch(ch["id"], is_seen))
            for ch in artist.get("youtube_channels", [])
        ] + [
            (f"wvp:{c['id']}", c["label"], lambda c=c: weverse.fetch_posts(c["id"]))
            for c in artist.get("weverse_communities", [])
        ] + [
            (f"ig:{a['username']}", a["label"], lambda a=a: instagram.fetch_posts(a))
            for a in artist.get("instagram_accounts", [])
            if ig_due
        ]
        communities = {f"wvp:{c['id']}": c for c in artist.get("weverse_communities", [])}
        for source, label, fetch in feeds:
            try:
                videos = fetch()
            except instagram.Throttled as e:
                logging.info("%s", e)
                continue
            except Exception as e:
                logging.warning("抓 %s 失敗：%s", label, e)
                continue

            initialized = db.execute("SELECT 1 FROM sources WHERE source=?", (source,)).fetchone()
            if not initialized:
                # 第一次看到這個頻道：既有影片只記錄、不推
                if not dry_run:
                    db.executemany(
                        "INSERT OR IGNORE INTO seen VALUES (?,?,?,?,?)",
                        [(v["uid"], source, v["title"], None, int(time.time())) for v in videos],
                    )
                    db.execute("INSERT INTO sources VALUES (?,1)", (source,))
                    db.commit()
                logging.info("初始化 %s：記錄 %d 則既有內容，不推播", label, len(videos))
                if source in communities:
                    check_comments(db, artist, communities[source], videos, dry_run)
                continue

            new = [v for v in videos if not db.execute("SELECT 1 FROM seen WHERE uid=?", (v["uid"],)).fetchone()]
            for v in sorted(new, key=lambda x: x["published"]):
                is_post = v["uid"].startswith(("wvp:", "ig:"))
                platform = "Instagram" if v["uid"].startswith("ig:") else "Weverse"
                text_in = v["body"] if is_post else v["title"]
                try:
                    zh = translate(text_in, post=is_post) if text_in.strip() else None
                except TranslateError as e:
                    ALERT.fail(db, source, v, e)
                    continue
                logging.info("新%s %s | %s → %s", "貼文" if is_post else "影片", label, v["title"], zh)
                if dry_run:
                    continue
                try:
                    if is_post:
                        msg_id = send_weverse_post(artist["name"], v, zh, artist.get("hashtags"), platform)
                        db.execute("INSERT OR REPLACE INTO tg_messages VALUES (?,?)", (v["uid"], msg_id))
                    else:
                        send_video(artist["name"], v, zh, artist.get("hashtags"))
                except Exception as e:
                    logging.warning("推播失敗，下輪重試：%s", e)
                    continue
                th_tags = artist.get("hashtags", [])[:1]  # Threads 一篇只吃一個主題標籤，其餘只是文字
                if is_post:
                    th_text = format_weverse_post(artist["name"], v, zh, th_tags,
                                                  max_len=notify_threads.TEXT_LIMIT, more="…（全文見連結）",
                                                  platform=platform)
                    enqueue_threads(db, v["uid"], th_text, v.get("photos"))
                else:
                    th_text = format_post(artist["name"], v, zh, th_tags)
                    enqueue_threads(db, v["uid"], th_text[: notify_threads.TEXT_LIMIT],
                                    [v["thumbnail"]] if v.get("thumbnail") else None)
                db.execute("INSERT INTO seen VALUES (?,?,?,?,?)", (v["uid"], source, v["title"], zh, int(time.time())))
                ALERT.clear(db, v["uid"])
                db.commit()

            if source in communities:
                check_comments(db, artist, communities[source], videos, dry_run)

    if not dry_run:
        flush_threads(db)
        ALERT.end_round()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    load_env()
    db = open_db()
    interval = int(os.environ.get("POLL_SECONDS", "300"))
    while True:
        run_once(db, args.dry_run)
        if args.once or args.dry_run:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
