"""追星機器人：輪詢來源（YouTube／Weverse 成員貼文＋官方公告／IG）→ 翻譯 → 推 Telegram → 同步 Threads。

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
import ig_publish
import review
import bot_watchdog
import live_archive
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
    # 留言 uid → 推播時那一則的第一個 uid（合併推播時 Threads 只記第一個），之後有人回這則留言就接在它底下
    db.execute("CREATE TABLE IF NOT EXISTS comment_heads (uid TEXT PRIMARY KEY, head TEXT)")
    # 翻譯失敗次數；到上限就放棄那則（記 seen、不推）
    db.execute("CREATE TABLE IF NOT EXISTS translate_fail (uid TEXT PRIMARY KEY, attempts INTEGER)")
    live_archive.init_db(db)
    ig_publish.init_db(db)  # IG Reels 排程發佈
    review.init_db(db)  # Threads 發文前審核
    return db


THREADS_MAX_ATTEMPTS = 5
SEQ_RESERVE = 8  # Threads 留言編號「💬N\n」預留的字數


def _comment_seq(db: sqlite3.Connection, root: str) -> int:
    """這篇貼文底下下一則留言的編號（發成功才寫回 kv，失敗重試號碼不變、被略過不跳號）。"""
    row = db.execute("SELECT v FROM kv WHERE k=?", (f"th_seq:{root}",)).fetchone()
    return int(row[0]) + 1 if row else 1


_PLATFORM = {"yt": "youtube", "wvp": "weverse", "wvn": "weverse", "wvl": "weverse", "ig": "instagram"}


def enqueue_threads(db: sqlite3.Connection, uid: str, text: str, images: list[str] | None = None,
                    reply_to_uid: str | None = None, root: str | None = None) -> None:
    """root：成員留言所屬的 Weverse 貼文 uid，發 Threads 時依它編號（💬1、💬2…）。"""
    if not notify_threads.enabled():
        return
    payload = {"text": text, "images": images or []}
    if root:
        payload["root"] = root
    payload = json.dumps(payload, ensure_ascii=False)
    db.execute("INSERT OR IGNORE INTO threads_queue VALUES (?,?,?,0,?)", (uid, payload, reply_to_uid, int(time.time())))


def flush_threads(db: sqlite3.Connection) -> None:
    if not notify_threads.enabled():
        return
    uids = [r[0] for r in db.execute("SELECT uid FROM threads_queue ORDER BY created").fetchall()]
    for uid in uids:
        # 每則現讀：這輪中途可能被刪或換掉（改寫／只發評論換掉了續段，新續段沿用 #2、#3 的 uid）
        row = db.execute("SELECT payload, reply_to_uid, attempts FROM threads_queue WHERE uid=?", (uid,)).fetchone()
        if not row:
            continue
        payload, reply_to_uid, attempts = row
        reply_media = None
        new_parts = None
        p = json.loads(payload)
        if reply_to_uid:
            if db.execute("SELECT 1 FROM threads_queue WHERE uid=?", (reply_to_uid,)).fetchone():
                continue  # 原貼文還沒發出去（或還在審核），等它
            if review.decision(db, reply_to_uid)[0] == "skipped":
                db.execute("DELETE FROM threads_queue WHERE uid=?", (uid,))  # 原貼文被略過，留言／續段也不發
                db.commit()
                continue
            row = db.execute("SELECT media_id FROM threads_posted WHERE uid=?", (reply_to_uid,)).fetchone()
            reply_media = row[0] if row else None
            if not reply_media and reply_to_uid.startswith("wvc:"):
                # 要接的留言沒發上 Threads（原貼文被略過等）→ 這則沒引用原句、單獨發看不懂，不發
                logging.info("Threads 略過 %s（被回覆的留言沒發過 Threads）", uid)
                db.execute("DELETE FROM threads_queue WHERE uid=?", (uid,))
                db.commit()
                continue
        else:
            status, comment = review.decision(db, uid)
            if status in review.PENDING:
                continue  # 等使用者在 TG 按鈕
            if status == "skipped":
                db.execute("DELETE FROM threads_queue WHERE uid=?", (uid,))
                db.commit()
                continue
            # 套用審核結果：評論（放最前面）、改寫全文、去標題／只發評論／拿掉分隔線
            p["text"], new_parts = review.final(db, uid, p["text"])
            if new_parts is not None:  # 續段換掉：先刪舊的，發成功再排新的
                db.execute("DELETE FROM threads_queue WHERE uid LIKE ?", (f"{uid}#%",))
                db.commit()
        seq = _comment_seq(db, p["root"]) if p.get("root") else None
        if seq:  # 成員留言依實際發出順序編號，方便在 Threads 上照順序讀（使用者 2026-11-11 決定）
            p["text"] = f"💬{seq}\n{p['text']}"[: notify_threads.TEXT_LIMIT]
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
        if seq:
            db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (f"th_seq:{p['root']}", str(seq)))
        prev = uid
        for i, part in enumerate(new_parts or [], start=2):  # 改寫後的續段，下一輪接在回覆
            db.execute("INSERT OR IGNORE INTO threads_queue VALUES (?,?,?,0,?)", (f"{uid}#{i}", json.dumps(
                {"text": part, "images": []}, ensure_ascii=False), prev, int(time.time())))
            prev = f"{uid}#{i}"
        db.commit()
        review.mark_posted(db, uid, media_id)


COMMENT_WINDOW_DAYS = 7
COMMENT_HOLD_MINUTES = float(os.environ.get("COMMENT_HOLD_MINUTES", "60"))  # 其他成員直接留言等多久沒人回才推
_ig_last_poll = 0.0  # IG 輪詢比其他來源慢（IG_POLL_SECONDS），避免吃光 Meta 的限流額度
_comments_last_poll: dict[str, float] = {}  # 成員留言每 COMMENT_POLL_SECONDS 抓一次（預設 15 分），成員連回的幾句才合得成一則


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
    if community.get("comments") is False:
        # config 關掉成員留言：不抓不推；清掉初始化紀錄，之後再打開會重新初始化（既有留言只記 seen、不洗版）
        if not dry_run and db.execute("DELETE FROM sources WHERE source=?", (source,)).rowcount:
            db.commit()
        return
    initialized = db.execute("SELECT 1 FROM sources WHERE source=?", (source,)).fetchone()
    if initialized and time.time() - _comments_last_poll.get(source, 0) < int(os.environ.get("COMMENT_POLL_SECONDS", "900")) - 30:
        return
    _comments_last_poll[source] = time.time()
    cutoff = time.time() - COMMENT_WINDOW_DAYS * 86400
    recent = [p for p in posts if datetime.fromisoformat(p["published"]).timestamp() >= cutoff]
    comments = []
    for p in recent:
        try:
            comments += [(p, c) for c in weverse.fetch_artist_comments(p["uid"][4:])]
        except Exception as e:
            logging.warning("抓留言失敗 %s：%s", p["uid"], e)

    if not initialized:
        if not dry_run:
            db.executemany("INSERT OR IGNORE INTO seen VALUES (?,?,?,?,?)",
                           [(c["uid"], source, c["body"][:80], None, int(time.time())) for _, c in comments])
            db.execute("INSERT INTO sources VALUES (?,1)", (source,))
            db.commit()
        logging.info("初始化 %s 留言：記錄 %d 則既有留言，不推播", community["label"], len(comments))
        return

    new = [(p, c) for p, c in comments if not db.execute("SELECT 1 FROM seen WHERE uid=?", (c["uid"],)).fetchone()]
    groups: dict[tuple, list] = {}  # 同一位成員回同一則留言 → 合成一則推（同一輪內）
    for p, c in sorted(new, key=lambda x: x[1]["created"]):
        # 原貼文我們沒推過（上線前既有、或翻譯放棄）→ 留言也不推，只記 seen（使用者 2026-11-03 決定：沒脈絡的留言不發）
        # 原貼文還沒 seen = 翻譯／推播失敗等下輪重試 → 這則留言也等，下輪再處理
        if not db.execute("SELECT 1 FROM tg_messages WHERE uid=?", (p["uid"],)).fetchone():
            if db.execute("SELECT 1 FROM seen WHERE uid=?", (p["uid"],)).fetchone() and not dry_run:
                db.execute("INSERT INTO seen VALUES (?,?,?,?,?)", (c["uid"], source, c["body"][:80], None, int(time.time())))
                db.commit()
                logging.info("留言略過（原貼文沒推過）%s | %s", c["author"], c["body"][:40])
            continue
        rt = c["reply_to"]
        # 成員回粉絲留言 → 整則不推（TG／Threads 都不發），只記 seen；回其他成員照推（使用者 2026-11-06 決定）
        skip = "成員回粉絲" if rt and not rt.get("author") else None
        if not rt and c["author"] != p.get("author"):
            # 其他成員直接留言：先等 COMMENT_HOLD_MINUTES，期間有成員回它就不單獨推（回覆那則會帶出這句），沒人回才推
            if any((x["reply_to"] or {}).get("id") and f"wvc:{x['reply_to']['id']}" == c["uid"] for _, x in comments):
                skip = "已有成員回覆，由回覆那則帶出"
            elif time.time() - c["created"] / 1000 < COMMENT_HOLD_MINUTES * 60:
                continue  # 還在等，下輪再看（不記 seen）
        if skip:
            if not dry_run:
                db.execute("INSERT INTO seen VALUES (?,?,?,?,?)", (c["uid"], source, c["body"][:80], None, int(time.time())))
                db.commit()
            logging.info("留言略過（%s）%s | %s", skip, c["author"], c["body"][:40])
            continue
        key = (p["uid"], c["author"], rt["id"]) if rt and rt.get("id") else (c["uid"],)
        groups.setdefault(key, (p, []))[1].append(c)

    for p, cs in groups.values():
        first = cs[0]
        # 被回覆的留言我們推過 → 接成討論串：TG／Threads 掛在那則留言底下，不再重複引用原句
        parent = f"wvc:{first['reply_to']['id']}" if first.get("reply_to") and first["reply_to"].get("id") else None
        parent_tg = db.execute("SELECT message_id FROM tg_messages WHERE uid=?", (parent,)).fetchone() if parent else None
        threaded = bool(parent_tg)
        try:
            zhs = [translate(c["body"], post=True) for c in cs]
            zh_rt = translate(first["reply_to"]["body"], post=True) if first.get("reply_to") and not threaded else None
        except TranslateError as e:
            ALERT.fail(db, source, {"uid": first["uid"], "title": first["body"], "url": p["url"]}, e)
            continue
        for c, zh in zip(cs, zhs):
            logging.info("新留言 %s | %s → %s", c["author"], c["body"], zh)
        if len(cs) > 1:
            logging.info("合併 %d 則同一留言的回覆成一則", len(cs))
        if dry_run:
            continue
        if threaded:
            row = parent_tg
            head = db.execute("SELECT head FROM comment_heads WHERE uid=?", (parent,)).fetchone()
            th_parent = head[0] if head else parent
            logging.info("接在已推過的留言底下 %s", parent)
        else:
            row = db.execute("SELECT message_id FROM tg_messages WHERE uid=?", (p["uid"],)).fetchone()
            th_parent = p["uid"]
        text = format_weverse_comment(cs, zhs, zh_rt, quote=not threaded)
        try:
            msg_id = send_weverse_comment(text, row[0] if row else None)
        except Exception as e:
            logging.warning("留言推播失敗，下輪重試：%s", e)
            continue
        th_text = format_weverse_comment(cs, zhs, zh_rt, limit=notify_threads.TEXT_LIMIT - SEQ_RESERVE, quote=not threaded)
        enqueue_threads(db, first["uid"], th_text, reply_to_uid=th_parent, root=p["uid"])
        for c, zh in zip(cs, zhs):
            db.execute("INSERT OR REPLACE INTO tg_messages VALUES (?,?)", (c["uid"], msg_id))
            db.execute("INSERT OR REPLACE INTO comment_heads VALUES (?,?)", (c["uid"], first["uid"]))
            db.execute("INSERT INTO seen VALUES (?,?,?,?,?)", (c["uid"], source, c["body"][:80], zh, int(time.time())))
            ALERT.clear(db, c["uid"])
        db.commit()


def run_once(db: sqlite3.Connection, dry_run: bool) -> None:
    from notify import format_post, send_live, send_video, send_weverse_post, split_threads_post
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
            (f"wvn:{c['id']}", f"{c['label']} 官方公告", lambda c=c: weverse.fetch_notices(c["id"], is_seen))
            for c in artist.get("weverse_communities", [])
            if c.get("notices")
        ] + [
            (f"wvl:{c['id']}", f"{c['label']} 直播", lambda c=c: weverse.fetch_lives(c["id"]))
            for c in artist.get("weverse_communities", [])
            if c.get("lives")
        ] + [
            (f"ig:{a['username']}", a["label"], lambda a=a: instagram.fetch_posts(a))
            for a in artist.get("instagram_accounts", [])
            if ig_due
        ]
        communities = {f"wvp:{c['id']}": c for c in artist.get("weverse_communities", [])}
        # Threads 要轉發哪些平台（youtube／weverse／instagram）；沒寫 = 全部。成員留言跟著 weverse
        th_sources = set(artist.get("threads_sources") or ["youtube", "weverse", "instagram"])
        for source, label, fetch in feeds:
            # 直播通知只推 TG 頻道，不發 Threads／FB
            threads_ok = _PLATFORM[source.split(":")[0]] in th_sources and not source.startswith("wvl:")
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
                is_post = v["uid"].startswith(("wvp:", "wvn:", "ig:"))
                platform = "Instagram" if v["uid"].startswith("ig:") else "Weverse"
                text_in = v["body"] if is_post else v["title"]
                try:
                    zh = translate(text_in, post=is_post) if text_in.strip() else None
                except TranslateError as e:
                    ALERT.fail(db, source, v, e)
                    continue
                logging.info("新%s %s | %s → %s", "直播" if v.get("kind") == "live" else "貼文" if is_post else "影片", label, v["title"], zh)
                if dry_run:
                    continue
                try:
                    if v.get("kind") == "live":
                        send_live(artist["name"], v, zh, artist.get("hashtags"))
                        live_archive.enqueue(db, v)  # 回放出來後自動存檔（.env 有設 LIVE_ARCHIVE_DIR 才會）
                    elif is_post:
                        msg_id = send_weverse_post(artist["name"], v, zh, artist.get("hashtags"), platform)
                        db.execute("INSERT OR REPLACE INTO tg_messages VALUES (?,?)", (v["uid"], msg_id))
                    else:
                        send_video(artist["name"], v, zh, artist.get("hashtags"))
                except Exception as e:
                    logging.warning("推播失敗，下輪重試：%s", e)
                    continue
                th_tags = artist.get("hashtags", [])[:1]  # Threads 一篇只吃一個主題標籤，其餘只是文字
                if not threads_ok:
                    pass  # 這個平台不發 Threads（config 的 threads_sources），只推 TG
                elif is_post:
                    # 超過 500 字不截斷：切成多段，第 2 段起接在前一段底下當回覆
                    parts = split_threads_post(artist["name"], v, zh, th_tags, platform, notify_threads.TEXT_LIMIT)
                    prev = None
                    for i, part in enumerate(parts):
                        uid = v["uid"] if i == 0 else f"{v['uid']}#{i + 1}"
                        enqueue_threads(db, uid, part, v.get("photos") if i == 0 else None, reply_to_uid=prev)
                        prev = uid
                    if notify_threads.enabled():  # 發 Threads 前先私訊使用者審核（可加評論）
                        review.ask(db, v["uid"], parts[0], len(parts), len(v.get("photos") or []), notify_threads.TEXT_LIMIT)
                else:
                    th_text = format_post(artist["name"], v, zh, th_tags)
                    enqueue_threads(db, v["uid"], th_text[: notify_threads.TEXT_LIMIT],
                                    [v["thumbnail"]] if v.get("thumbnail") else None)
                db.execute("INSERT INTO seen VALUES (?,?,?,?,?)", (v["uid"], source, v["title"], zh, int(time.time())))
                ALERT.clear(db, v["uid"])
                db.commit()

            if source in communities:
                check_comments(db, artist, communities[source], videos, dry_run)
            if source.startswith("wvl:") and not dry_run:
                live_archive.step(db, videos)

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
    if not (args.once or args.dry_run):
        bot_watchdog.start()  # 主迴圈卡住太久就結束，Docker 自動重開
    while True:
        bot_watchdog.kick()
        run_once(db, args.dry_run)
        if args.once or args.dry_run:
            break
        try:
            ig_publish.run(db)  # 時間到的 IG 排程短影音
        except Exception as e:
            logging.warning("IG 排程檢查失敗：%s", e)
        nxt = ig_publish.seconds_to_next(db)  # 有排程快到了就提早醒來，準時發
        review.wait(db, interval if nxt is None else min(interval, nxt + 1))  # 期間長輪詢 TG，處理審核按鈕／評論


if __name__ == "__main__":
    main()
