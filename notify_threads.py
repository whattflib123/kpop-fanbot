"""Threads 發文（Meta 官方 Threads API）。

流程：建立 container → 等 status=FINISHED → threads_publish。
多張圖 = 每張先建 is_carousel_item container，再建 CAROUSEL container。
回覆（成員留言）= 建 TEXT container 時帶 reply_to_id。

token：
- 第一次從 .env 的 THREADS_ACCESS_TOKEN（長效 token，60 天）讀。
- 之後存在 data/threads_token.json，每 7 天自動 refresh（token 要滿 24 小時才能 refresh）。
"""
import json
import logging
import os
import time
from pathlib import Path

import requests

API = "https://graph.threads.net/v1.0"
TEXT_LIMIT = 500
CAROUSEL_MAX = 20
REFRESH_EVERY = 7 * 86400
TOKEN_FILE = Path(os.environ.get("DB_PATH", Path(__file__).parent / "data" / "state.db")).parent / "threads_token.json"


def enabled() -> bool:
    return bool(os.environ.get("THREADS_ACCESS_TOKEN") or TOKEN_FILE.exists())


def _load_token() -> str:
    if TOKEN_FILE.exists():
        state = json.loads(TOKEN_FILE.read_text())
    else:
        state = {"token": os.environ["THREADS_ACCESS_TOKEN"], "refreshed_at": time.time()}
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(state))
    if time.time() - state["refreshed_at"] > REFRESH_EVERY:
        try:
            r = requests.get("https://graph.threads.net/refresh_access_token",
                             params={"grant_type": "th_refresh_token", "access_token": state["token"]}, timeout=20)
            r.raise_for_status()
            state = {"token": r.json()["access_token"], "refreshed_at": time.time()}
            TOKEN_FILE.write_text(json.dumps(state))
            TOKEN_FILE.chmod(0o600)
            logging.info("Threads token 已 refresh")
        except Exception as e:
            logging.warning("Threads token refresh 失敗（舊 token 還能用到過期）：%s", _mask(e))
    return state["token"]


def _mask(e: Exception) -> str:
    """錯誤訊息可能帶完整 URL（含 access_token），遮掉。"""
    import re
    return re.sub(r"access_token=[^&\s]+", "access_token=***", str(e))


def _req(method: str, path: str, **params) -> dict:
    params["access_token"] = _load_token()
    r = requests.request(method, f"{API}/{path}", params=params, timeout=30)
    data = r.json()
    if r.status_code >= 400 or "error" in data:
        raise RuntimeError(f"Threads {path} 失敗：{data.get('error', {}).get('message', data)}")
    return data


def _wait(container_id: str, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = _req("GET", container_id, fields="status,error_message")
        if st.get("status") == "FINISHED":
            return
        if st.get("status") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Threads container {st.get('status')}：{st.get('error_message')}")
        time.sleep(5)
    raise RuntimeError("Threads container 處理逾時")


def publish(text: str, images: list[str] | None = None, reply_to_id: str | None = None) -> str:
    """發一則 Threads，回傳 media id。"""
    images = (images or [])[:CAROUSEL_MAX]
    me = "me"
    params = {"text": text[:TEXT_LIMIT]}
    if reply_to_id:
        params["reply_to_id"] = reply_to_id
    if len(images) >= 2:
        children = []
        for url in images:
            c = _req("POST", f"{me}/threads", media_type="IMAGE", image_url=url, is_carousel_item="true")["id"]
            children.append(c)
        for c in children:
            _wait(c)
        cid = _req("POST", f"{me}/threads", media_type="CAROUSEL", children=",".join(children), **params)["id"]
    elif len(images) == 1:
        cid = _req("POST", f"{me}/threads", media_type="IMAGE", image_url=images[0], **params)["id"]
    else:
        cid = _req("POST", f"{me}/threads", media_type="TEXT", **params)["id"]
    _wait(cid)
    return _req("POST", f"{me}/threads_publish", creation_id=cid)["id"]


def whoami() -> dict:
    return _req("GET", "me", fields="id,username,name")
