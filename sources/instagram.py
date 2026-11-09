"""Instagram 貼文（Meta Graph API Business Discovery）。

用自己的 IG 專業帳號（META_IG_USER_ID，連在粉專上）＋粉專 token（META_PAGE_TOKEN，不會過期）
查別人的專業／創作者帳號最新貼文。限時動態抓不到（API 不提供別人的 story）。

限流：開發模式 app 的額度很小，而且 Business Discovery 巢狀查詢很耗「處理時間」（x-app-usage 的 total_time），
比呼叫次數先爆。所以 IG 輪詢比較慢（main.py 的 IG_POLL_SECONDS）、每次只抓最近幾則，
並在 x-app-usage 任一項 ≥ USAGE_SKIP_PCT 時先跳過，不要硬撞到 (#4) Application request limit reached。
"""
import json
import logging
import os
import re
import time

import requests

G = "https://graph.facebook.com/v21.0"
MEDIA_FIELDS = "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,children{media_type,media_url,thumbnail_url}"


USAGE_SKIP_PCT = 80
USAGE_TTL = 15 * 60  # 用量讀數多久內有效（Meta 是滾動一小時）
_usage = {"pct": 0, "at": 0.0}


class Throttled(RuntimeError):
    """用量太高，這輪先不抓。"""


def usage_pct() -> int:
    return _usage["pct"] if time.time() - _usage["at"] < USAGE_TTL else 0


def _record_usage(resp: requests.Response) -> None:
    try:
        u = json.loads(resp.headers.get("x-app-usage", "{}"))
    except ValueError:
        return
    if u:
        _usage.update(pct=max(int(v) for v in u.values()), at=time.time())
        if _usage["pct"] >= USAGE_SKIP_PCT:
            logging.warning("IG API 用量 %s，暫停抓 IG", u)


def _mask(s: str) -> str:
    return re.sub(r"access_token=[^&\s]+", "access_token=***", str(s))


def fetch_posts(account: dict, limit: int = 5) -> list[dict]:
    """account = {"username": ..., "author": ...}，回傳新到舊。用量太高丟 Throttled。"""
    if usage_pct() >= USAGE_SKIP_PCT:
        raise Throttled(f"IG API 用量 {usage_pct()}%，跳過 {account['username']}")
    r = requests.get(f"{G}/{os.environ['META_IG_USER_ID']}", timeout=30, params={
        "access_token": os.environ["META_PAGE_TOKEN"],
        "fields": f"business_discovery.username({account['username']}){{media.limit({limit}){{{MEDIA_FIELDS}}}}}",
    })
    _record_usage(r)
    data = r.json()
    if "error" in data:
        if data["error"].get("code") in (4, 17, 32, 613):  # 各種限流
            _usage.update(pct=max(_usage["pct"], 100), at=time.time())
            raise Throttled(f"IG {account['username']} 被限流：{_mask(data['error'].get('message'))}")
        raise RuntimeError(f"IG {account['username']}：{_mask(data['error'].get('message'))}")
    posts = []
    for m in data["business_discovery"].get("media", {}).get("data", []):
        items = m.get("children", {}).get("data") or [m]
        photos = [x.get("media_url") if x.get("media_type") == "IMAGE" else x.get("thumbnail_url") for x in items]
        caption = m.get("caption") or ""
        posts.append({
            "uid": f"ig:{m['id']}",
            "author": account["author"],
            "title": caption[:80],
            "body": caption,
            "photos": [p for p in photos if p],
            # TG 用：照原順序的照片／影片（影片傳不了時退回 photos 的縮圖）
            "media": [{"type": "video" if x.get("media_type") == "VIDEO" else "photo", "url": x["media_url"]}
                      for x in items if x.get("media_url")],
            "has_video": m.get("media_type") == "VIDEO" or any(x.get("media_type") == "VIDEO" for x in items),
            "url": m["permalink"],
            # Graph API 給 +0000，換成 fromisoformat 吃得下的 +00:00
            "published": re.sub(r"\+0000$", "+00:00", m["timestamp"]),
        })
    return posts
