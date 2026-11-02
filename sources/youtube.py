"""YouTube 頻道新影片。

主要：官方 RSS（免 API key，最新 15 支）。
備援：RSS 常偶發 404／500，重試仍失敗就改抓頻道 /videos、/shorts 頁面的 ytInitialData。
頁面列得比 RSS 多、又沒有發布時間，所以備援只取「比已知最新一支還新」的影片（每分頁最多 5 支），
避免把舊影片當新的推出去。
"""
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
PAGE_URL = "https://www.youtube.com/channel/{}/{}"
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
    "Accept-Language": "ko,en;q=0.8",
}
FALLBACK_MAX_PER_TAB = 5


def fetch(channel_id: str, is_seen: Callable[[str], bool] | None = None) -> list[dict]:
    try:
        return _fetch_rss(channel_id)
    except Exception as e:
        logging.warning("YouTube RSS 失敗，改抓頻道頁面：%s", e)
    return _fetch_page(channel_id, is_seen)


def _fetch_rss(channel_id: str) -> list[dict]:
    for attempt in range(3):
        resp = requests.get(FEED_URL.format(channel_id), timeout=20)
        if resp.ok:
            break
        time.sleep(2 * (attempt + 1))
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    videos = []
    for entry in root.findall("atom:entry", NS):
        vid = entry.findtext("yt:videoId", namespaces=NS)
        link = entry.find("atom:link", NS)
        url = link.get("href") if link is not None else f"https://www.youtube.com/watch?v={vid}"
        thumb = entry.find("media:group/media:thumbnail", NS)
        videos.append({
            "uid": f"yt:{vid}",
            "title": entry.findtext("atom:title", namespaces=NS) or "",
            "url": url,
            "published": entry.findtext("atom:published", namespaces=NS) or "",
            "thumbnail": thumb.get("url") if thumb is not None else None,
            "is_short": "/shorts/" in url,
        })
    return videos


def _find(obj, key: str, out: list) -> list:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            _find(v, key, out)
    elif isinstance(obj, list):
        for v in obj:
            _find(v, key, out)
    return out


def _initial_data(channel_id: str, tab: str) -> dict:
    html = requests.get(PAGE_URL.format(channel_id, tab), headers=PAGE_HEADERS,
                        cookies={"CONSENT": "YES+1"}, timeout=20).text
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html)
    if not m:
        raise RuntimeError(f"頻道頁面 /{tab} 找不到 ytInitialData")
    return json.loads(m.group(1))


def _page_items(channel_id: str) -> list[tuple[str, list[dict]]]:
    """[(分頁, [{vid, title, is_short}, ...])]，各分頁新到舊。"""
    tabs = []
    data = _initial_data(channel_id, "videos")
    tabs.append(("videos", [
        {"vid": x["contentId"], "title": x["metadata"]["lockupMetadataViewModel"]["title"]["content"], "is_short": False}
        for x in _find(data, "lockupViewModel", [])
        if x.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO"
    ]))
    try:
        data = _initial_data(channel_id, "shorts")
        items = []
        for x in _find(data, "shortsLockupViewModel", []):
            vid = (x.get("onTap", {}).get("innertubeCommand", {}).get("reelWatchEndpoint", {}) or {}).get("videoId")
            if vid:
                items.append({"vid": vid, "title": x["overlayMetadata"]["primaryText"]["content"], "is_short": True})
        tabs.append(("shorts", items))
    except Exception as e:
        logging.warning("頻道頁面 /shorts 失敗：%s", e)
    return tabs


def _fetch_page(channel_id: str, is_seen: Callable[[str], bool] | None) -> list[dict]:
    now = datetime.now(timezone.utc)
    videos = []
    for _, items in _page_items(channel_id):
        picked = []
        for it in items:
            uid = f"yt:{it['vid']}"
            if is_seen and is_seen(uid):
                break  # 遇到已知的就停：後面都是舊的
            picked.append(it)
            if len(picked) >= FALLBACK_MAX_PER_TAB:
                break
        for i, it in enumerate(picked):
            vid = it["vid"]
            videos.append({
                "uid": f"yt:{vid}",
                "title": it["title"],
                "url": f"https://www.youtube.com/shorts/{vid}" if it["is_short"] else f"https://www.youtube.com/watch?v={vid}",
                # 頁面沒有發布時間；用「現在 − 名次」維持新舊順序
                "published": (now - timedelta(seconds=i)).isoformat(),
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "is_short": it["is_short"],
            })
    return videos
