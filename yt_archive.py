"""官方 YouTube 長片自動存檔＋統整＋中文外掛字幕（2026-11-26）。

config.json 的 youtube_channels 裡標 "archive": true 的頻道（qwer-update 目前只有 QWER 官方），推完新影片通知後：
  1. 下載 1080p mp4＋韓文自動字幕（YouTube 語音辨識）＋info.json
  2. 用 Claude Code（`claude -p`，訂閱額度）讀韓文字幕產統整＋爆點（格式同 live_summary，見 README「官方 YT 存檔」）
  3. 把韓文字幕逐句直翻成繁中外掛字幕 srt（播放器跟 mp4 一起開就有中字）
  4. TG 私訊 admin：中文片名＋統整＋前幾個爆點
只做 NAS 做得到的事：不 OCR 燒在畫面上的韓文字卡、不燒字幕、不上傳 YT（那些等 Mac 的 video_toolkit 開工時做）。

存到 .env 的 YT_ARCHIVE_DIR（容器內路徑；NAS 上是 homes/yunyi_lee/media/qwer_youtube），每支同檔名
<YYYYMMDD>_<原標題>_<videoId>：.mp4、.info.json、.ko.srt（韓文自動字幕）、.zh_TW.srt（中文直翻）、.summary.json／.md。
Shorts、短於 YT_ARCHIVE_MIN_SEC（預設 180）秒、會員限定不存。只做新片；舊片手動：
  docker exec -d qwer-update python yt_archive.py work <videoId>
在背景子行程跑，一次一支；首播還沒開始、自動字幕還沒產生就隔 RETRY_SEC 再試，超過 RETRY_HOURS 放棄並私訊。
要 CLAUDE_CODE_OAUTH_TOKEN；YT_ARCHIVE=0 關閉。
"""
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import taste

RETRY_SEC = 30 * 60
RETRY_HOURS = 48
TR_CHUNK = 250  # 每次翻幾句
KST = timezone(timedelta(hours=9))
EXIT_SKIP, EXIT_LATER = 2, 3

_running: dict[str, tuple[subprocess.Popen, Path]] = {}  # uid → (行程, log 檔)


def enabled() -> bool:
    return (bool(os.environ.get("YT_ARCHIVE_DIR")) and bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
            and os.environ.get("YT_ARCHIVE", "1") != "0")


def min_sec() -> int:
    return int(os.environ.get("YT_ARCHIVE_MIN_SEC", "180"))


def init_db(db: sqlite3.Connection) -> None:
    # status：pending 待做（含等首播／等字幕）、done 好了、skipped 不存（太短、會員限定）、failed 放棄
    db.execute("CREATE TABLE IF NOT EXISTS yt_archive (uid TEXT PRIMARY KEY, title TEXT, name TEXT, status TEXT, "
               "attempts INTEGER DEFAULT 0, note TEXT, created INTEGER, updated INTEGER)")


def enqueue(db: sqlite3.Connection, video: dict) -> None:
    """main.py 推完新影片通知後呼叫（只有標 archive 的頻道）。Shorts 不存。"""
    if not enabled() or video.get("is_short"):
        return
    now = int(time.time())
    db.execute("INSERT OR IGNORE INTO yt_archive (uid, title, status, created, updated) VALUES (?,?,?,?,?)",
               (video["uid"], video.get("title", ""), "pending", now, 0))


def step(db: sqlite3.Connection) -> None:
    """每輪呼叫：收跑完的、有到期的待做項目就開一個背景子行程。"""
    if not enabled():
        return
    from notify import send_admin

    folder = Path(os.environ["YT_ARCHIVE_DIR"])
    now = int(time.time())
    for uid, (proc, log) in list(_running.items()):
        if proc.poll() is None:
            continue
        del _running[uid]
        out = log.read_text(errors="replace") if log.exists() else ""
        log.unlink(missing_ok=True)
        title, created = db.execute("SELECT title, created FROM yt_archive WHERE uid=?", (uid,)).fetchone()
        lines = out.strip().splitlines() or ["(沒有輸出)"]
        tail = lines[-1]
        if proc.returncode == 0:
            name = next((ln[5:].strip() for ln in reversed(lines) if ln.startswith("NAME ")), None)
            db.execute("UPDATE yt_archive SET status='done', name=?, note=NULL, updated=? WHERE uid=?", (name, now, uid))
            logging.info("官方 YT 存檔＋統整完成 %s", name)
            if name:
                _admin(send_admin, done_message(folder, name))
        elif proc.returncode == EXIT_SKIP:
            db.execute("UPDATE yt_archive SET status='skipped', note=?, updated=? WHERE uid=?", (tail[:300], now, uid))
            logging.info("官方 YT 不存 %s：%s", title, tail)
        else:
            logging.warning("官方 YT 存檔沒完成 %s（%s）：%s", title, "稍後再試" if proc.returncode == EXIT_LATER else "失敗", tail)
            db.execute("UPDATE yt_archive SET note=?, updated=? WHERE uid=?", (tail[:300], now, uid))
            if now - created > RETRY_HOURS * 3600:
                db.execute("UPDATE yt_archive SET status='failed', updated=? WHERE uid=?", (now, uid))
                _admin(send_admin, f"⚠️ 官方 YT 存檔放棄（試了 {RETRY_HOURS} 小時）：{title}\n{tail[:300]}")
        db.commit()

    if _running:
        return
    row = db.execute("SELECT uid, title, attempts FROM yt_archive WHERE status='pending' AND updated<=? "
                     "ORDER BY created", (now - RETRY_SEC,)).fetchone()
    if not row:
        return
    uid, title, attempts = row
    db.execute("UPDATE yt_archive SET attempts=?, updated=? WHERE uid=?", (attempts + 1, now, uid))
    db.commit()
    folder.mkdir(parents=True, exist_ok=True)
    logging.info("開始官方 YT 存檔 %s", title)
    log = folder / f".{uid[3:]}.log"
    with log.open("w") as f:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "work", uid[3:]],
                                stdout=f, stderr=subprocess.STDOUT, umask=0)
    _running[uid] = (proc, log)


def _admin(send_admin, text: str) -> None:
    try:
        send_admin(text[:4000])
    except Exception as e:
        logging.warning("官方 YT 存檔通知失敗：%s", e)


def done_message(folder: Path, name: str) -> str:
    try:
        s = json.loads((folder / f"{name}.summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"📼 官方 YT 已存檔：{name}"
    v = s["video"]
    out = [f"📼 官方 YT 已存檔＋統整：{v['title_zh']}", f"（{v['title_ko']}，{_ts(v['duration_sec'])}）", "",
           s["summary"], "", "爆點："]
    for h in s["highlights"][:5]:
        out.append(f"{h['id']}. {_ts(h['start'])}–{_ts(h['end'])} {h['title']}（{'★' * h['score']}）")
    out += ["", f"NAS media/qwer_youtube/{name}（.mp4＋.zh_TW.srt 中文外掛字幕、.summary.md）", v["url"]]
    return "\n".join(out)


# ───── 子行程：下載＋統整＋翻譯 ─────

def _safe(s: str, n: int) -> str:
    s = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", s).strip()
    return s[:n].strip() or "untitled"


def _ts(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _srt_ts(ms: int) -> str:
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"


def probe(url: str) -> dict:
    r = subprocess.run(["yt-dlp", "-J", "--no-warnings", "--no-playlist", url], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        err = (r.stderr.strip().splitlines() or ["(沒有輸出)"])[-1]
        if re.search(r"members|join this channel|Private video", err, re.I):
            print(f"會員限定／私人影片，不存：{err}")
            sys.exit(EXIT_SKIP)
        if re.search(r"Premieres in|will begin in|live event", err, re.I):
            print(f"首播／直播還沒開始，稍後再試：{err}")
            sys.exit(EXIT_LATER)
        raise SystemExit(f"yt-dlp 讀不到影片資訊：{err}")
    return json.loads(r.stdout)


def cues_from_json3(path: Path) -> list[dict]:
    """YouTube 自動字幕 json3 → [{start, end（毫秒）, ko}]。換行事件（aAppend）跳過、空句和連續重複去掉。"""
    events = json.loads(path.read_text(encoding="utf-8")).get("events", [])
    raw = []
    for e in events:
        if not e.get("segs") or e.get("aAppend"):
            continue
        text = re.sub(r"\s+", " ", "".join(s.get("utf8", "") for s in e["segs"])).strip()
        if text and not (raw and raw[-1]["ko"] == text):
            raw.append({"start": int(e["tStartMs"]), "end": int(e["tStartMs"]) + int(e.get("dDurationMs", 2000)), "ko": text})
    for a, b in zip(raw, raw[1:]):
        a["end"] = min(a["end"], b["start"])  # 自動字幕會疊兩行，外掛字幕一次只顯示一句
    return raw


_VTT_TIME = re.compile(r"(\d+):(\d\d):(\d\d)[.,](\d{3})\s+-->\s+(\d+):(\d\d):(\d\d)[.,](\d{3})")


def cues_from_vtt(path: Path) -> list[dict]:
    """YouTube 有的影片字幕只給 vtt（沒有 json3）→ [{start, end（毫秒）, ko}]。
    滾動式自動字幕（每格重複上一句）只取新的那行；連續重複去掉。"""
    def ms(h, m, s, f):
        return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(f)

    raw: list[dict] = []
    block: list[str] = []

    def flush():
        if not block:
            return
        m = _VTT_TIME.match(block[0])
        if not m:
            return
        lines = [re.sub(r"<[^>]+>", "", x).strip() for x in block[1:]]
        lines = [x for x in lines if x]
        if raw and len(lines) > 1 and lines[0] == raw[-1]["ko"].split("\n")[-1]:
            lines = lines[1:]  # 滾動式：第一行是上一格的
        text = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if text and not (raw and raw[-1]["ko"] == text):
            raw.append({"start": ms(*m.groups()[:4]), "end": ms(*m.groups()[4:]), "ko": text})

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            flush()
            block = []
        elif block or _VTT_TIME.match(line):
            block.append(line)
    flush()
    for a, b in zip(raw, raw[1:]):
        a["end"] = min(a["end"], b["start"])
    return raw


def load_cues(path: Path) -> list[dict]:
    return cues_from_json3(path) if path.suffix == ".json3" else cues_from_vtt(path)


def write_srt(path: Path, cues: list[dict], key: str) -> None:
    body = "\n".join(f"{i}\n{_srt_ts(c['start'])} --> {_srt_ts(c['end'])}\n{c[key]}\n" for i, c in enumerate(cues, 1))
    _write(path, body)


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o666)  # 容器是 root；讓 NAS 帳號也能刪改
    os.replace(tmp, path)


def download(url: str, folder: Path, name: str) -> Path | None:
    """下載 mp4（已存在就跳過）＋韓文自動字幕（json3，沒有就 vtt）。回傳字幕路徑；還沒有自動字幕回 None。"""
    mp4 = folder / f"{name}.mp4"
    base = ["yt-dlp", "--no-progress", "--no-playlist", "--no-warnings", "--no-mtime", "-P", str(folder), "-o", f"{name}.%(ext)s"]
    subs = base + ["--skip-download", "--write-auto-subs", "--sub-langs", "ko", "--sub-format", "json3/vtt", url]
    r = subprocess.run(subs, capture_output=True, text=True, timeout=600, umask=0)
    if r.returncode != 0:
        raise SystemExit(f"字幕下載失敗：{(r.stderr.strip().splitlines() or ['?'])[-1]}")
    if not mp4.exists():
        vid = base + ["-f", "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]",
                      "--merge-output-format", "mp4", "--write-info-json", url]
        r = subprocess.run(vid, capture_output=True, text=True, timeout=3 * 3600, umask=0)
        if r.returncode != 0 or not mp4.exists():
            raise SystemExit(f"影片下載失敗：{(r.stderr.strip().splitlines() or ['?'])[-1]}")
        print(f"影片存好：{mp4.name}（{mp4.stat().st_size / 2**20:.0f} MB）")
    for ext in ("json3", "vtt"):  # 有的影片只給 vtt
        p = folder / f"{name}.ko.{ext}"
        if p.exists():
            return p
    return None


def summary_prompt(n: int) -> str:
    import live_summary as ls
    glossary = json.loads((Path(__file__).parent / "glossary.json").read_text(encoding="utf-8"))
    names = "、".join(f"{k} → {v}" for k, v in glossary.items())
    return (
        f"你是 K-pop 綜藝精華企劃，幫台灣的翻譯剪輯團隊（{ls.artist_name()} 粉絲頻道）整理 {ls.artist_name()} 官方 YouTube 頻道的影片（綜藝、幕後、vlog）。\n"
        "輸入是整支影片的韓文自動字幕（YouTube 語音辨識），每行「時間 句子」，時間是影片中的時間點。\n"
        "全部用台灣繁體中文寫，語氣自然。\n\n"
        "要產出：\n"
        "1. title_zh：影片標題的中文翻譯（emoji 保留）。\n"
        "2. summary：整支統整 3～6 句：什麼企劃、在哪裡、做什麼、誰出演、氣氛、有沒有重要消息。\n"
        "3. segments：照話題／企劃環節切段，從頭到尾覆蓋整支，每段一句話說在做什麼。\n"
        f"4. highlights：挑 {n} 個最值得剪成中字精華的片段（素材真的不夠可以少，但至少 1 個），照精彩程度由高到低排。\n"
        "   - 優先：成員互相吐槽、爆料、反應大的段落；料理、教學、閒聊有反轉或笑點一樣可以挑。\n"
        "   - 每個都要有頭有尾：setup＝前因（怎麼聊到的、鋪陳），payoff＝爆點本身，after＝後續反應或收尾（沒有就 null）。"
        "觀眾只看這段也要看得懂。\n"
        "   - 時間一律用輸入裡真的有的時間點，不要編。\n"
        "   - fit：setup 開頭到結尾 60 秒內講得完＝short（適合 Shorts），需要更長＝long。\n"
        "   - why：用粉絲的角度說為什麼好笑、可愛、好哭或有料。\n"
        "   - quotes：2～5 句關鍵原話；ko 照字幕原文，zh 照字直翻成台灣口語，不加油添醋、不裝可愛。\n"
        "   - members_mentioned：這段出現或被提到的成員；沒有就空陣列。\n"
        "   - source：主要靠字幕內容挑的＝sub；靠聲音峰值挑的＝audio；兩者都有＝both。\n"
        "   - score：1～5，5＝一定要剪。\n\n"
        "注意：\n"
        "- 這是語音辨識字幕：常聽錯字、斷句怪、不知道誰在講、BGM 大的地方會漏。照上下文理解；quotes 的 ko 保留字幕原樣，"
        "覺得聽錯的在 why 註明「字幕可能聽錯」。\n"
        "- 官方影片畫面上有燒進去的字卡（旁白、吐槽、說明），通常字幕裡沒有；有的影片字幕會用 [ ] 或 ( ) 附上字卡內容，有就一起參考。"
        "你看不到畫面，純畫面的笑點不要猜。\n"
        "- 字幕後面附「聲音峰值」：音量比前後 30 秒突然高出很多的時間點（大笑、尖叫、驚呼等），附那幾秒的字幕。"
        "字幕平淡但聲音很大的段落可能是反應笑點，可以列進 highlights，why 註明「聲音峰值」並寫高出幾 dB；"
        "不確定就照實說「聲音很大，要看畫面確認」。音樂、音效造成的峰值不要列。\n"
        "- 只用字幕裡有的內容，不要用你自己知道的背景知識補劇情。\n"
        f"- 名字照詞彙表：{names}。{ls.name_hint()}\n"
        + taste.prompt_block()
    )


def translate_prompt() -> str:
    import live_summary as ls
    glossary = json.loads((Path(__file__).parent / "glossary.json").read_text(encoding="utf-8"))
    names = "、".join(f"{k} → {v}" for k, v in glossary.items())
    return (
        f"你是 K-pop 中字翻譯，把 {ls.artist_name()} 官方 YouTube 影片的韓文自動字幕（語音辨識）逐句翻成台灣繁體中文外掛字幕。\n"
        "輸入每行「編號<TAB>韓文」，照順序是同一支影片連續的字幕；標「前文」的只給你看上下文，不用翻。\n"
        "規則：\n"
        "- 每個編號都要回一句 zh，編號照輸入。照字直翻成台灣口語，不加油添醋、不裝可愛、不加註解。\n"
        "- 自動字幕常把一句話切在兩行中間：可以在相鄰兩行之間挪字，讓每行讀起來通順，但不要合併或漏掉編號。\n"
        "- 聽錯的字照上下文猜最合理的意思；完全無意義的雜訊（例：[음악]、單獨的語助詞）zh 給空字串。\n"
        "- 喊人的「야」翻「呀」，不要翻「喂」。\n"
        "- [ ] 是畫面字卡、( ) 是旁白或補充：照樣保留括號，字卡用［ ］、旁白用（ ）。\n"
        f"- 名字照詞彙表：{names}。{ls.name_hint()}\n"
    )


TR_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["lines"],
    "properties": {"lines": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["i", "zh"],
        "properties": {"i": {"type": "integer"}, "zh": {"type": "string"}}}}},
}


def translate_cues(cues: list[dict]) -> None:
    """就地填 cues[*]['zh']。分段送，每段附前 10 句當上下文。"""
    for a in range(0, len(cues), TR_CHUNK):
        b = min(len(cues), a + TR_CHUNK)
        ctx = "\n".join(f"前文\t{c['ko']}" for c in cues[max(0, a - 10):a])
        body = "\n".join(f"{i}\t{cues[i]['ko']}" for i in range(a, b))
        out = _ask(translate_prompt(), (ctx + "\n" if ctx else "") + body, TR_SCHEMA)
        got = {x["i"]: x["zh"].strip() for x in out["lines"]}
        missing = [i for i in range(a, b) if i not in got]
        if len(missing) > (b - a) // 10:
            raise SystemExit(f"翻譯漏太多句（{len(missing)}/{b - a}）")
        for i in range(a, b):
            cues[i]["zh"] = got.get(i, "")
        print(f"翻譯 {b}/{len(cues)} 句")


def _ask(system: str, text: str, schema: dict) -> dict:
    """同 live_summary.ask，但 schema 可換（live_summary.ask 寫死它自己的 SCHEMA）。"""
    import live_summary
    cmd = [
        "claude", "-p", "--model", live_summary.MODEL, "--system-prompt", system,
        "--tools", "", "--strict-mcp-config", "--setting-sources", "",
        "--disable-slash-commands", "--no-session-persistence",
        "--output-format", "json", "--json-schema", json.dumps(schema),
    ]
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=live_summary.CLI_TIMEOUT, env=env)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise SystemExit(f"claude 輸出不是 JSON（exit {proc.returncode}）：{(proc.stderr or proc.stdout)[:300]}") from None
    if proc.returncode != 0 or data.get("is_error"):
        raise SystemExit(f"claude 回報錯誤：{str(data.get('result'))[:300]}")
    out = data.get("structured_output")
    if not isinstance(out, dict):
        raise SystemExit(f"沒拿到結構化輸出（stop_reason={data.get('stop_reason')}）")
    print(f"claude 完成：{data.get('duration_ms', 0) / 1000:.0f} 秒")
    return out


def summarize(folder: Path, name: str, info: dict, cues: list[dict]) -> dict:
    import live_summary as ls
    lines = [(c["start"] // 1000, c["ko"]) for c in cues]
    duration = float(info.get("duration") or lines[-1][0])
    peaks = []
    try:
        levels = ls.loudness(folder / f"{name}.mp4")
        for p in ls.find_peaks(levels):  # 自動字幕跟 mp4 同一條時間軸，不用對時
            a, b = int(p["start"]), int(p["end"] + 0.999)
            text = " ".join(s for t, s in lines if a - 2 <= t <= b + 1) or None
            peaks.append({"start": a, "end": b, "start_ts": _ts(a), "end_ts": _ts(b), "db_over": p["db_over"],
                          "has_sub": text is not None, "text": text})
        audio_note = f"聲音峰值 {len(peaks)} 段"
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
        audio_note = f"聲音分析失敗：{e}"
    print(audio_note)

    date = datetime.strptime(info["upload_date"], "%Y%m%d").strftime("%Y-%m-%d") if info.get("upload_date") else ""
    header = f"頻道：{info.get('channel', '')}\n標題：{info.get('title', '')}\n上傳日期：{date}\n長度：{_ts(duration)}\n"
    if info.get("description"):
        header += f"說明欄：{info['description'][:800]}\n"
    body = "\n".join(f"{_ts(t)} {s}" for t, s in lines)
    if peaks:
        body += "\n\n聲音峰值（+dB＝比前後 30 秒平常音量高出多少）：\n" + "\n".join(
            f"{p['start_ts']}–{p['end_ts']} +{p['db_over']}dB：{p['text'] or '無字幕'}" for p in peaks)
    data = _ask(summary_prompt(ls.n_highlights(duration)), header + "\n字幕：\n" + body, ls.SCHEMA)

    def conv(part):
        if part is None:
            return None
        return {"start": ls._sec(part["start"]), "end": ls._sec(part["end"]),
                "start_ts": part["start"], "end_ts": part["end"], "zh": part["zh"]}

    highlights = []
    for i, h in enumerate(data["highlights"], 1):
        parts = [p for p in (h["setup"], h["payoff"], h["after"]) if p]
        highlights.append({
            "id": i, "title": h["title"], "score": h["score"], "fit": h["fit"],
            "start": min(ls._sec(p["start"]) for p in parts), "end": max(ls._sec(p["end"]) for p in parts),
            "setup": conv(h["setup"]), "payoff": conv(h["payoff"]), "after": conv(h["after"]),
            "why": h["why"],
            "quotes": [{"t": ls._sec(q["t"]), "t_ts": q["t"], "ko": q["ko"], "zh": q["zh"]} for q in h["quotes"]],
            "members_mentioned": h["members_mentioned"], "source": h["source"],
        })
    return {
        "version": 1,
        "kind": "youtube",
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "model": ls.MODEL,
        "video": {
            "name": name, "video_id": info["id"], "channel": info.get("channel", ""),
            "title_ko": info.get("title", ""), "title_zh": data["title_zh"], "date": date,
            "duration_sec": int(duration), "url": info.get("webpage_url", ""), "thumbnail": info.get("thumbnail", ""),
            "files": {"mp4": f"{name}.mp4", "ko_srt": f"{name}.ko.srt", "zh_srt": f"{name}.zh_TW.srt",
                      "info": f"{name}.info.json"},
        },
        "time_base": "mp4",
        "time_note": "時間就是 mp4 時間（YouTube 自動字幕跟影片同一條時間軸），不用對時",
        "audio_note": audio_note,
        "summary": data["summary"],
        "segments": [{"start": ls._sec(s["start"]), "end": ls._sec(s["end"]), "start_ts": s["start"], "end_ts": s["end"],
                      "topic": s["topic"]} for s in data["segments"]],
        "highlights": highlights,
        "audio_peaks": peaks,
    }


def to_markdown(s: dict) -> str:
    v = s["video"]
    out = [f"# {v['date']} {v['title_zh']}", "", f"- 原標題：{v['title_ko']}", f"- 長度：{_ts(v['duration_sec'])}",
           f"- 連結：{v['url']}", f"- 中文外掛字幕：`{v['files']['zh_srt']}`（語音辨識直翻，畫面上燒的字卡沒翻）",
           "", "## 統整", "", s["summary"], "", "## 段落", ""]
    out += [f"- `{x['start_ts']}–{x['end_ts']}` {x['topic']}" for x in s["segments"]]
    out += ["", "## 爆點", ""]
    for h in s["highlights"]:
        out += [f"### {h['id']}. {h['title']}（{'★' * h['score']}，{'Shorts' if h['fit'] == 'short' else '長片'}）",
                f"`{_ts(h['start'])}–{_ts(h['end'])}`", ""]
        for label, key in (("前因", "setup"), ("爆點", "payoff"), ("後續", "after")):
            p = h[key]
            if p:
                out.append(f"- **{label}** `{p['start_ts']}–{p['end_ts']}`：{p['zh']}")
        out.append(f"- **為什麼**：{h['why']}")
        if h["members_mentioned"]:
            out.append(f"- **成員**：{'、'.join(h['members_mentioned'])}")
        out += [f"  - `{q['t_ts']}` {q['ko']} → {q['zh']}" for q in h["quotes"]]
        out.append("")
    return "\n".join(out)


def work(vid: str, folder: Path | None = None) -> str:
    folder = folder or Path(os.environ["YT_ARCHIVE_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    url = f"https://www.youtube.com/watch?v={vid}"
    info = probe(url)
    if info.get("live_status") in ("is_upcoming", "is_live", "post_live"):
        print(f"首播／直播中（{info['live_status']}），稍後再試")
        sys.exit(EXIT_LATER)
    if (info.get("duration") or 0) < min_sec():
        print(f"片長 {info.get('duration')} 秒，短於 {min_sec()} 秒，不存")
        sys.exit(EXIT_SKIP)
    if info.get("availability") in ("subscriber_only", "premium_only", "needs_auth", "private"):
        print(f"{info['availability']}，不存")
        sys.exit(EXIT_SKIP)
    day = info.get("upload_date") or datetime.now(KST).strftime("%Y%m%d")
    name = f"{day}_{_safe(info.get('title', ''), 50)}_{vid}"
    print(f"NAME {name}")

    j3 = download(url, folder, name)
    if j3 is None:
        print("還沒有韓文自動字幕，稍後再試（影片已存）")
        sys.exit(EXIT_LATER)
    cues = load_cues(j3)
    if len(cues) < 5:
        print(f"自動字幕太少（{len(cues)} 句），稍後再試")
        sys.exit(EXIT_LATER)
    write_srt(folder / f"{name}.ko.srt", cues, "ko")
    info_path = folder / f"{name}.info.json"
    if info_path.exists():
        info = json.loads(info_path.read_text(encoding="utf-8"))

    summary = folder / f"{name}.summary.json"
    if not summary.exists():
        s = summarize(folder, name, info, cues)
        _write(folder / f"{name}.summary.md", to_markdown(s))
        _write(summary, json.dumps(s, ensure_ascii=False, indent=2))
        print(f"統整好了：{len(s['segments'])} 段、{len(s['highlights'])} 個爆點")
    zh = folder / f"{name}.zh_TW.srt"
    if not zh.exists():
        translate_cues(cues)
        write_srt(zh, [c for c in cues if c.get("zh")], "zh")
        print(f"中文字幕好了：{sum(1 for c in cues if c.get('zh'))} 句")
    j3.unlink(missing_ok=True)
    print(f"NAME {name}")
    return name


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "work":
        work(sys.argv[2], Path(sys.argv[3]) if len(sys.argv) > 3 else None)
    else:
        sys.exit("用法：python yt_archive.py work <videoId> [資料夾]")
