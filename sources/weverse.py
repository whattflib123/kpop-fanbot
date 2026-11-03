"""Weverse 成員貼文（Artist 分頁）：網頁版 API，免登入。

Media 分頁幾乎都是 YouTube 轉貼，跟 YouTube 來源重複，所以不抓。

API 要 HMAC 簽章：wmd = base64(HMAC-SHA1(key, path_with_query[:255] + 毫秒 timestamp))，
key 寫在 weverse.io 前端 JS 裡。改版換 key 時回 401 wam_401，這裡會自動重抓 key 再試一次。
"""
import base64
import hashlib
import hmac
import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone

import requests

BASE = "https://global.apis.naver.com/weverse/wevweb"
COMMON = {"appId": "be4d79eb8fc7bd008ee82c8ec4ff6fd4", "language": "zh-tw", "os": "WEB", "platform": "WEB", "wpf": "pc"}
HEADERS = {"Referer": "https://weverse.io/", "Origin": "https://weverse.io", "User-Agent": "Mozilla/5.0"}
_key = b"1b9cb6378d959b45714bec49971ade22e6e24e42"


def _refresh_key() -> bool:
    """從 weverse.io 前端 JS 找新的 HMAC key（40 位 hex）。"""
    global _key
    page = requests.get("https://weverse.io/", headers=HEADERS, timeout=20).text
    queue = re.findall(r'src="(https://cdn-v2pstatic\.weverse\.io/[^"]+\.js)"', page)
    seen = set()
    while queue and len(seen) < 40:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        js = requests.get(url, timeout=20).text
        if "generateHmacParams" in js or "wmsgpad" in js or "HmacConfigParam" in js:
            keys = set(re.findall(r'"([0-9a-f]{40})"', js))
            if len(keys) == 1:
                _key = keys.pop().encode()
                logging.info("Weverse HMAC key 已更新")
                return True
        base = url.rsplit("/", 1)[0]
        queue += [f"{base}/{m}" for m in re.findall(r'(?:assets/|\./)([A-Za-z0-9_-]+\.js)', js)]
    return False


def _get(path: str, params: dict) -> dict:
    for attempt in range(2):
        p = f"{path}?{urllib.parse.urlencode({**COMMON, **params})}"
        ts = str(int(time.time() * 1000))
        wmd = base64.b64encode(hmac.new(_key, (p[:255] + ts).encode(), hashlib.sha1).digest()).decode()
        url = f"{BASE}{p}&wmsgpad={ts}&wmd={urllib.parse.quote(wmd, safe='')}"
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code == 401 and attempt == 0 and _refresh_key():
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Weverse HMAC 驗證失敗")


def community_id(url_path: str) -> int:
    """weverse.io/<url_path> → communityId。"""
    return _get("/community/v1.0/communityIdUrlPathByUrlPathArtistCode/preview", {"keyword": url_path})["communityId"]


def fetch_posts(cid: int, limit: int = 20) -> list[dict]:
    data = _get(f"/post/v1.0/community-{cid}/artistTabPosts", {"fieldSet": "postsV1", "limit": str(limit), "pagingType": "CURSOR"})
    posts = []
    for p in data.get("data", []):
        author = p.get("author") or {}
        official = (author.get("artistOfficialProfile") or {}).get("officialName")
        atts = p.get("orderedAttachments") or []
        posts.append({
            "uid": f"wvp:{p['postId']}",
            "author": official or author.get("profileName") or "",
            "body": (p.get("plainBody") or "").strip(),
            "title": (p.get("plainBody") or "").strip()[:80],  # 存 DB 用
            "photos": [a["data"]["url"] for a in atts if a.get("type") == "photo" and a.get("data", {}).get("url")],
            "has_video": any(a.get("type") == "video" for a in atts),
            "url": p["shareUrl"].split("?")[0],
            "published": datetime.fromtimestamp(p["publishedAt"] / 1000, timezone.utc).isoformat(),
            "membership_only": bool(p.get("membershipOnly") or p.get("locked")),
        })
    return posts


def _name(author: dict) -> str:
    return (author.get("artistOfficialProfile") or {}).get("officialName") or author.get("profileName") or ""


def fetch_artist_comments(post_id: str) -> list[dict]:
    """某篇貼文底下的成員留言（含成員回覆別人的留言）。post_id 不含 wvp: 前綴。"""
    data = _get(f"/comment/v1.0/post-{post_id}/artistComments", {"fieldSet": "postArtistCommentsV1", "limit": "50"})
    out = []
    for c in data.get("data", []):
        parent = c.get("parent") or {}
        pdata = parent.get("data") or {}
        pauthor = pdata.get("author") or {}
        reply_to = None
        if parent.get("type") == "COMMENT" and pdata.get("body"):
            reply_to = {
                "id": pdata.get("commentId"),  # 同一輪成員連回同一則留言時用來合併
                "body": pdata["body"].strip(),
                "author": _name(pauthor) if pauthor.get("profileType") == "ARTIST" else None,  # 粉絲不留暱稱
            }
        out.append({
            "uid": f"wvc:{c['commentId']}",
            "post_uid": f"wvp:{post_id}",
            "author": _name(c.get("author") or {}),
            "body": (c.get("body") or "").strip(),
            "created": c.get("createdAt", 0),
            "reply_to": reply_to,
        })
    return out


def fetch_notices(cid: int, is_seen, limit: int = 10) -> list[dict]:
    """官方公告（Notice 分頁）。列表只有標題，內文要再打一次 detail，所以看過的只回 uid／title 不抓內文。
    會員限定公告跳過（抓不到內文）。回傳格式同 fetch_posts，多 kind="notice"、author="官方"。"""
    data = _get(f"/notice/v1.0/community-{cid}/notices", {"fieldSet": "noticesV1", "limit": str(limit), "pagingType": "CURSOR"})
    out = []
    for n in data.get("data", []):
        if not n.get("exposed", True) or n.get("membershipOnly"):
            continue
        uid = f"wvn:{n['noticeId']}"
        title = (n.get("title") or "").strip()
        item = {
            "uid": uid, "kind": "notice", "author": "官方", "title": title[:80], "body": "", "photos": [], "has_video": False,
            "url": n["shareUrl"].split("?")[0],
            "published": datetime.fromtimestamp(n["publishAt"] / 1000, timezone.utc).isoformat(),
            "membership_only": False,
        }
        if not is_seen(uid):
            d = _get(f"/notice/v1.0/notice-{n['noticeId']}", {"fieldSet": "noticeV1"})
            body = (d.get("plainBody") or "").strip()
            item["body"] = f"{title}\n\n{body}" if title and not body.startswith(title) else body or title
            photos = ((d.get("attachment") or {}).get("photo") or {}).values()
            item["photos"] = [p["url"] for p in photos if p.get("url")]
        out.append(item)
    return out
