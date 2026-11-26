"""統整（summary.json）→ TG HTML 兩則：先「整場統整＋大綱」、再「爆點列表」（使用者 2026-11-26 定的列法）。

- 爆點列表：4～5 星最多 10 個（照時間、粗體標題、可複製時間、前因／後續掛底下），其餘摺疊；使用者回編號挑。
- 排版：區塊間 `----------------`（上下各空一行）、統整每句一段、大綱段落間空一行。
- 時間一律換成回放 mp4 時間（直播 summary 的 time_base 是 vtt，要扣 mp4_offset_sec）。
bot（live_summary／yt_archive 統整完）和 Mac 的 video_toolkit `translate.py picks` 共用這份。
"""
import html
import re

SEP = "----------------"
TG_LIMIT = 4096


def _ts_fn(s: dict):
    off = (s.get("mp4_offset_sec") or 0) if s.get("time_base") == "vtt" else 0

    def ts(x):
        x = max(0, int(x - off))
        h, m, sec = x // 3600, x % 3600 // 60, x % 60
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"
    return ts, off


def _head(s: dict) -> tuple[str, str]:
    ts, off = _ts_fn(s)
    v = s.get("live") or s.get("video") or {}
    who = f"{v.get('member')} 直播" if s.get("live") else (s.get("source_label") or v.get("channel") or "官方 YT")
    return f"{v.get('date', '')} {who}〈{v.get('title_ko', '')}〉（{ts(v.get('duration_sec', 0) + off)}）", v.get("title_zh", "")


def outline_html(s: dict, footer: list[str] | None = None) -> str:
    """整場統整（每句一段）＋大綱（話題段落）。footer：最後附的純文字行（例：NAS 路徑、連結）。"""
    e = html.escape
    ts, _ = _ts_fn(s)
    head, zh = _head(s)
    sents = [x for x in re.split(r"(?<=[。！？])", s.get("summary", "")) if x.strip()]
    L = [f"<b>{e(head)}</b>", f"<i>{e(zh)}</i>", "", SEP, "", "<b>整場統整</b>", ""]
    for x in sents:
        L += [e(x.strip()), ""]
    L += [SEP, "", f"<b>大綱（{len(s.get('segments', []))} 段，時間＝回放影片時間）</b>", ""]
    for g in s.get("segments", []):
        L += [f"<code>{ts(g['start'])}</code>　{e(g['topic'])}", ""]
    if footer:
        L += [SEP, ""] + [e(x) for x in footer]
    return "\n".join(L).rstrip()


def picks_html(s: dict, top_n: int = 10) -> str | None:
    """爆點列表；舊版統整沒有 candidates 回 None。"""
    e = html.escape
    ts, off = _ts_fn(s)
    c = s.get("candidates") or []
    if not c:
        return None
    by = {x["id"]: x for x in c}
    top = sorted([x for x in c if x["score"] >= 4], key=lambda x: (-x["score"], x["start"]))[:top_n]
    top.sort(key=lambda x: x["start"])
    kids = {t["id"]: sorted((by[i] for i in t.get("related", []) if i in by and by[i]["score"] < 4), key=lambda x: x["start"])
            for t in top}
    used = {t["id"] for t in top} | {k["id"] for v in kids.values() for k in v}
    others = [x for x in c if x["id"] not in used]
    num, n = {}, 0
    for t in top:
        n += 1; num[t["id"]] = n
        for k in kids[t["id"]]:
            n += 1; num[k["id"]] = n
    for x in others:
        n += 1; num[x["id"]] = n
    head, zh = _head(s)
    L = [f"<b>{e(head)}</b>", f"<i>{e(zh)}</i>", "<i>時間＝回放影片時間，點時間可複製</i>", "",
         f"<b>★ 4～5 星（{len(top)} 個）</b>", "", SEP, ""]
    for t in top:
        L += [f"<b>{num[t['id']]}. {e(t['title'])}</b>",
              f"<code>{ts(t['start'])}–{ts(t['end'])}</code>　{'★' * t['score']}　{'Shorts' if t['fit'] == 'short' else '長片'}",
              f"└ {e(t['why'])}"]
        for k in kids[t["id"]]:
            tag = "前因" if k["end"] <= t["start"] else "後續" if k["start"] >= t["end"] else "同一件事"
            L.append(f"　＋{tag} {num[k['id']]}. <code>{ts(k['start'])}</code> {'★' * k['score']} {e(k['title'])}")
        L += ["", SEP, ""]  # 分隔線上下各空一行（使用者 2026-11-26）
    tail = ["", "回編號就好，例：3、6"]
    if others:
        rows = [f"{num[x['id']]}. <code>{ts(x['start'])}</code> {'★' * x['score']} {e(x['title'])}" for x in others]
        base = len("\n".join(L + tail)) + 120
        cut = len(rows)
        while cut and base + len("\n".join(rows[:cut])) > TG_LIMIT:
            cut -= 1  # 候選太多（長片 100 多個）就截掉後面的，指去 .summary.md
        more = [f"…還有 {len(rows) - cut} 個在 .summary.md"] if cut < len(rows) else []
        L += ["", f"<b>其餘 {len(others)} 個</b>（點開展開）",
              "<blockquote expandable>" + "\n".join(rows[:cut] + more) + "</blockquote>"]
    return "\n".join(L + tail)


def messages(s: dict, top_n: int = 10, footer: list[str] | None = None) -> list[str]:
    """要傳的訊息（先大綱、再爆點列表）；爆點列表超過 TG 上限就減少 4～5 星個數。"""
    o = outline_html(s, footer)
    out = [o if len(o) <= TG_LIMIT else o[:TG_LIMIT - 20].rsplit("\n", 1)[0] + "\n…（太長截斷）"]
    p = picks_html(s, top_n)
    if p:
        out.append(p)
    return out
