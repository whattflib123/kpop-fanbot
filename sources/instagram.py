"""Instagram 貼文（Meta Graph API Business Discovery）。

用自己的 IG 專業帳號（META_IG_USER_ID，連在粉專上）＋粉專 token（META_PAGE_TOKEN，不會過期）
查別人的專業／創作者帳號最新貼文。限制：每個 IG 帳號每小時約 200 次呼叫；5 帳號 × 每 5 分鐘 = 60 次。
限時動態抓不到（API 不提供別人的 story）。
"""
import os
import re

import requests

G = "https://graph.facebook.com/v21.0"
MEDIA_FIELDS = "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,children{media_type,media_url,thumbnail_url}"


def _mask(s: str) -> str:
    return re.sub(r"access_token=[^&\s]+", "access_token=***", str(s))


def fetch_posts(account: dict, limit: int = 10) -> list[dict]:
    """account = {"username": ..., "author": ...}，回傳新到舊。"""
    r = requests.get(f"{G}/{os.environ['META_IG_USER_ID']}", timeout=30, params={
        "access_token": os.environ["META_PAGE_TOKEN"],
        "fields": f"business_discovery.username({account['username']}){{media.limit({limit}){{{MEDIA_FIELDS}}}}}",
    })
    data = r.json()
    if "error" in data:
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
            "has_video": m.get("media_type") == "VIDEO" or any(x.get("media_type") == "VIDEO" for x in items),
            "url": m["permalink"],
            # Graph API 給 +0000，換成 fromisoformat 吃得下的 +00:00
            "published": re.sub(r"\+0000$", "+00:00", m["timestamp"]),
        })
    return posts
