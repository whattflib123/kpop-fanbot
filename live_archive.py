"""Weverse 直播回放自動存檔：推過開播通知的直播，等回放（type=VOD）出來就用 yt-dlp 下載。

存到 .env 的 LIVE_ARCHIVE_DIR（容器內路徑；NAS 上是 homes/yunyi_lee/media/weverse_live）；沒設就不存。
每支：影片（https 直連 mp4，最高畫質）＋韓文／繁中官方字幕 vtt＋info.json。
yt-dlp 在背景子行程跑，一次一支，不卡主迴圈；容器重啟時沒下完的會從 .part 續傳。
回放剛結束可能還在轉檔 → 下載失敗就下一輪再試，超過 RETRY_HOURS 才放棄並私訊通知。
會員限定的直播不存（沒登入抓不到）。

補存舊回放（在容器裡跑，一支一支下載，已經有 mp4 的跳過，可以中斷後重跑）：
  docker exec -d qwer-update python live_archive.py backfill 164   # 164 = communityId
進度寫在存檔資料夾的 .backfill.log，跑完私訊 admin。
"""
import logging
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

RETRY_HOURS = 24
_running: dict[str, tuple[subprocess.Popen, float, Path]] = {}  # uid → (行程, 開始時間, log 檔)


def enabled() -> bool:
    return bool(os.environ.get("LIVE_ARCHIVE_DIR"))


def init_db(db: sqlite3.Connection) -> None:
    # status：pending 等回放／下載中、done 存好、failed 放棄、skipped 會員限定不存
    db.execute("CREATE TABLE IF NOT EXISTS live_archive (uid TEXT PRIMARY KEY, status TEXT, path TEXT, "
               "attempts INTEGER DEFAULT 0, created INTEGER, updated INTEGER)")


def enqueue(db: sqlite3.Connection, live: dict) -> None:
    """推完開播通知後呼叫，排進待存檔。"""
    if not enabled():
        return
    status = "skipped" if live.get("membership_only") else "pending"
    now = int(time.time())
    db.execute("INSERT OR IGNORE INTO live_archive (uid, status, created, updated) VALUES (?,?,?,?)",
               (live["uid"], status, now, now))


def _safe(s: str, n: int) -> str:
    s = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", s).strip()
    return s[:n].strip() or "untitled"


def _cmd(live: dict, folder: Path) -> tuple[list[str], str]:
    day = live["published"][:10].replace("-", "")
    name = f"{day}_{_safe(live['author'], 20)}_{_safe(live['title'], 50)}_{live['uid'][4:]}"
    return [
        "yt-dlp", "--no-progress", "--no-playlist",
        "-f", "b[protocol=https]/b", "-S", "res",
        "--write-auto-subs", "--sub-langs", "ko_KR,zh_TW", "--sub-format", "vtt",
        "--write-info-json", "--no-mtime",
        "-P", str(folder), "-o", f"{name}.%(ext)s",
        live["url"],
    ], name


def step(db: sqlite3.Connection, lives: list[dict]) -> None:
    """每輪抓完直播列表後呼叫：收已跑完的下載、回放出來的就開始下載（一次一支）。"""
    if not enabled():
        return
    from notify import send_admin

    folder = Path(os.environ["LIVE_ARCHIVE_DIR"])
    now = int(time.time())

    for uid, (proc, started, log) in list(_running.items()):
        if proc.poll() is None:
            continue
        del _running[uid]
        out = log.read_text(errors="replace") if log.exists() else ""
        log.unlink(missing_ok=True)
        row = db.execute("SELECT path, created FROM live_archive WHERE uid=?", (uid,)).fetchone()
        if proc.returncode == 0:
            db.execute("UPDATE live_archive SET status='done', updated=? WHERE uid=?", (now, uid))
            logging.info("直播存檔完成 %s（%.0f 秒）", row[0], time.time() - started)
            _admin(send_admin, f"📼 Weverse 直播回放已存檔：{row[0]}")
        else:
            tail = out.strip().splitlines()[-1:] or ["(沒有輸出)"]
            logging.warning("直播存檔失敗 %s：%s", uid, tail[0])
            if now - row[1] > RETRY_HOURS * 3600:
                db.execute("UPDATE live_archive SET status='failed', updated=? WHERE uid=?", (now, uid))
                _admin(send_admin, f"⚠️ Weverse 直播回放存檔放棄（試了 {RETRY_HOURS} 小時）：{row[0]}\n{tail[0][:300]}")
        db.commit()

    if _running:
        return
    by_uid = {v["uid"]: v for v in lives}
    for uid, attempts in db.execute("SELECT uid, attempts FROM live_archive WHERE status='pending' ORDER BY created").fetchall():
        live = by_uid.get(uid)
        if not live:
            continue  # 掉出列表前 10 支：通常不會發生（一場直播不會排這麼久）
        if live.get("live"):
            continue  # 還在直播，等回放
        folder.mkdir(parents=True, exist_ok=True)
        cmd, name = _cmd(live, folder)
        db.execute("UPDATE live_archive SET path=?, attempts=?, updated=? WHERE uid=?", (name, attempts + 1, now, uid))
        db.commit()
        logging.info("開始存檔直播回放 %s", name)
        log = folder / f".{uid[4:]}.log"
        with log.open("w") as f:  # 子行程繼承 fd，這裡關掉不影響它寫
            # 容器是 root，umask=0 讓 NAS 帳號從 SMB／File Station 也能刪改
            proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, umask=0)
        _running[uid] = (proc, time.time(), log)
        return


def _admin(send_admin, text: str) -> None:
    try:
        send_admin(text)
    except Exception as e:
        logging.warning("存檔通知失敗：%s", e)


def backfill(cid: int) -> None:
    from notify import send_admin
    from sources import weverse

    folder = Path(os.environ["LIVE_ARCHIVE_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=folder / ".backfill.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    db = sqlite3.connect(os.environ.get("DB_PATH", "data/state.db"))
    init_db(db)
    lives = [v for v in weverse.fetch_lives(cid, limit=50, all_pages=True) if not v["live"]]
    lives.sort(key=lambda v: v["published"], reverse=True)  # 新的先存
    done = ok = skipped = 0
    fails = []
    logging.info("補存開始：%d 支回放", len(lives))
    for v in lives:
        pid = v["uid"][4:]
        if v["membership_only"] or any(folder.glob(f"*_{pid}.mp4")):
            skipped += 1
            continue
        cmd, name = _cmd(v, folder)
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, umask=0)
        done += 1
        now = int(time.time())
        if r.returncode == 0:
            ok += 1
            db.execute("INSERT OR REPLACE INTO live_archive (uid, status, path, attempts, created, updated) VALUES (?,?,?,?,?,?)",
                       (v["uid"], "done", name, 1, now, now))
            db.commit()
            logging.info("[%d/%d] 存好 %s", done + skipped, len(lives), name)
        else:
            tail = (r.stdout.strip().splitlines() or ["(沒有輸出)"])[-1]
            fails.append(f"{name}：{tail[:150]}")
            logging.warning("[%d/%d] 失敗 %s：%s", done + skipped, len(lives), name, tail)
    msg = f"📼 Weverse 舊直播補存完成：存好 {ok} 支、跳過 {skipped} 支（已存／會員限定）、失敗 {len(fails)} 支"
    if fails:
        msg += "\n" + "\n".join(fails[:10])
    logging.info(msg)
    _admin(send_admin, msg)


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "backfill":
        backfill(int(sys.argv[2]))
    else:
        sys.exit("用法：python live_archive.py backfill <communityId>")
