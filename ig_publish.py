"""排程發 IG Reels（自己的 IG 專業帳號 META_IG_USER_ID，用粉專 token META_PAGE_TOKEN）。

排程表：data/ig_schedule/schedule.json（本機用 ig_schedule.py 新增／刪除，bot 只讀不寫）
  [{"id": "20261110-2000-xxx", "video": "20261110-2000-xxx.mp4", "at": "2026-11-10T20:00:00+08:00",
    "caption": "IG 文案", "threads": "（選填）Threads 文案", "cover_ms": 1500（選填，封面取第幾毫秒）}]
狀態記在 state.db 的 ig_scheduled（pending 沒紀錄 → posted／failed）。

時間到 → 建 REELS 容器（resumable 上傳，影片不用公開網址）→ 等處理完 → media_publish → 抓 permalink
→ 私訊 TELEGRAM_ADMIN_CHAT_ID：IG 連結（點進去用「分享」轉到 Threads）＋ Threads 文案（點一下複製）。
發成功後刪掉 NAS 上的影片檔省空間。失敗下一輪重試，IG_MAX_ATTEMPTS（預設 3）次後放棄並私訊。

權限：token 要有 instagram_content_publish（讀 IG 用的四個權限之外再加這個，重拿 user token 跑 meta_setup.py）。
"""
import html
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import requests

import notify

G = "https://graph.facebook.com/v21.0"
UPLOAD = "https://rupload.facebook.com/ig-api-upload/v21.0"
CAPTION_LIMIT = 2200
PROCESS_TIMEOUT = 15 * 60  # IG 處理影片最多等多久
RETRY_SEC = 10 * 60  # 失敗後隔多久重試


def sched_dir() -> Path:
    return Path(os.environ.get("DB_PATH", "data/state.db")).parent / "ig_schedule"


def enabled() -> bool:
    return bool(os.environ.get("META_PAGE_TOKEN") and os.environ.get("META_IG_USER_ID"))


def init_db(db: sqlite3.Connection) -> None:
    db.execute("CREATE TABLE IF NOT EXISTS ig_scheduled (id TEXT PRIMARY KEY, status TEXT, media_id TEXT,"
               " permalink TEXT, attempts INTEGER, error TEXT, updated INTEGER)")


def load() -> list[dict]:
    f = sched_dir() / "schedule.json"
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except ValueError as e:
        logging.warning("IG 排程表讀不到：%s", e)
        return []


def _state(db: sqlite3.Connection, sid: str) -> tuple[str, int]:
    row = db.execute("SELECT status, attempts FROM ig_scheduled WHERE id=?", (sid,)).fetchone()
    return (row[0], row[1] or 0) if row else ("pending", 0)


def _due_at(db: sqlite3.Connection, item: dict) -> float:
    """該發的時間；失敗過的要再隔 RETRY_SEC 才重試。"""
    at = datetime.fromisoformat(item["at"]).timestamp()
    row = db.execute("SELECT attempts, updated FROM ig_scheduled WHERE id=?", (item["id"],)).fetchone()
    return max(at, row[1] + RETRY_SEC) if row and row[0] else at


def pending(db: sqlite3.Connection) -> list[dict]:
    return [it for it in load() if _state(db, it["id"])[0] == "pending"]


def seconds_to_next(db: sqlite3.Connection) -> float | None:
    """下一支還沒發的排程離現在幾秒（已過期 = 0）；沒有就 None。main 用來把等待時間縮短，準時發。"""
    if not enabled():
        return None
    now = time.time()
    times = [_due_at(db, it) - now for it in pending(db)]
    return max(0.0, min(times)) if times else None


def _api(method: str, path: str, **params) -> dict:
    params["access_token"] = os.environ["META_PAGE_TOKEN"]
    resp = requests.request(method, f"{G}/{path}", data=params if method == "POST" else None,
                            params=params if method == "GET" else None, timeout=60)
    data = resp.json()
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"({err.get('code')}) {err.get('error_user_msg') or err.get('message')}")
    return data


def publish_reel(video: Path, caption: str, cover_ms: int | None = None) -> tuple[str, str]:
    """回 (media_id, permalink)。"""
    ig = os.environ["META_IG_USER_ID"]
    params = {"media_type": "REELS", "upload_type": "resumable", "caption": caption, "share_to_feed": "true"}
    if cover_ms is not None:
        params["thumb_offset"] = str(cover_ms)
    cid = _api("POST", f"{ig}/media", **params)["id"]
    with video.open("rb") as fh:
        resp = requests.post(f"{UPLOAD}/{cid}", data=fh, timeout=600, headers={
            "Authorization": f"OAuth {os.environ['META_PAGE_TOKEN']}",
            "offset": "0", "file_size": str(video.stat().st_size)})
    if not resp.ok or not resp.json().get("success"):
        raise RuntimeError(f"上傳影片失敗：{resp.status_code} {resp.text[:200]}")
    deadline = time.time() + PROCESS_TIMEOUT
    while True:
        st = _api("GET", cid, fields="status_code,status")
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"IG 處理影片失敗：{st.get('status')}")
        if time.time() > deadline:
            raise RuntimeError(f"IG 處理影片超過 {PROCESS_TIMEOUT // 60} 分鐘（{code}）")
        time.sleep(10)
    mid = _api("POST", f"{ig}/media_publish", creation_id=cid)["id"]
    try:
        link = _api("GET", mid, fields="permalink").get("permalink", "")
    except RuntimeError:
        link = ""
    return mid, link


def _notify_done(item: dict, link: str) -> None:
    lines = [f"✅ IG 短影音已發佈（排程 {item['at'][:16].replace('T', ' ')}）",
             html.escape(link) if link else "（拿不到連結，請到 IG 個人頁看）",
             "點連結 → 分享 → Threads 轉發"]
    if item.get("threads"):
        lines += ["", "Threads 文案（點一下複製）：", f"<pre>{html.escape(item['threads'])}</pre>"]
    notify._call("sendMessage", chat_id=os.environ["TELEGRAM_ADMIN_CHAT_ID"], text="\n".join(lines),
                 parse_mode="HTML", disable_web_page_preview=False)


def run(db: sqlite3.Connection) -> None:
    if not enabled():
        return
    max_attempts = int(os.environ.get("IG_MAX_ATTEMPTS", "3"))
    for item in pending(db):
        if _due_at(db, item) > time.time():
            continue
        sid = item["id"]
        _, attempts = _state(db, sid)
        video = sched_dir() / item["video"]
        try:
            if not video.exists():
                raise RuntimeError(f"找不到影片檔 {item['video']}")
            if len(item.get("caption", "")) > CAPTION_LIMIT:
                raise RuntimeError(f"文案超過 {CAPTION_LIMIT} 字")
            logging.info("IG 排程發佈 %s", sid)
            mid, link = publish_reel(video, item.get("caption", ""), item.get("cover_ms"))
        except Exception as e:
            attempts += 1
            status = "failed" if attempts >= max_attempts else "pending"
            db.execute("INSERT OR REPLACE INTO ig_scheduled VALUES (?,?,?,?,?,?,?)",
                       (sid, status, None, None, attempts, str(e)[:500], int(time.time())))
            db.commit()
            logging.warning("IG 排程 %s 失敗（第 %d 次）：%s", sid, attempts, e)
            if status == "failed":
                notify.send_admin(f"⚠️ IG 排程 {sid} 發佈失敗 {attempts} 次，放棄：{e}")
            continue
        db.execute("INSERT OR REPLACE INTO ig_scheduled VALUES (?,?,?,?,?,?,?)",
                   (sid, "posted", mid, link, attempts, None, int(time.time())))
        db.commit()
        logging.info("IG 已發 %s → %s %s", sid, mid, link)
        try:
            _notify_done(item, link)
        except Exception as e:
            logging.warning("IG 發佈通知失敗：%s", e)
        try:
            video.unlink()
        except OSError:
            pass
