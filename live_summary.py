"""Weverse 直播統整：回放存檔完成後，用 Claude Code（`claude -p`，訂閱額度）讀韓文字幕，
產出整場統整＋爆點（前因／爆點／後續＋時間），給 video_toolkit 的 agent 接手剪精華。

輸出跟影片放同一個資料夾（LIVE_ARCHIVE_DIR），同檔名：
  <name>.summary.json   固定格式（version 1，欄位說明見 README「直播統整」）
  <name>.summary.md     給人看的版本
只做新直播（live_archive 存好才排進來）；舊的要手動：
  docker exec qwer-update python live_summary.py run "<name>"      # name = 不含副檔名的存檔檔名
在背景子行程跑，一次一場，不卡主迴圈；失敗下一輪重試，MAX_ATTEMPTS 次放棄並私訊 admin。
要 CLAUDE_CODE_OAUTH_TOKEN（走訂閱）；LIVE_SUMMARY=0 關閉。
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

MODEL = "claude-opus-5-5"
CLI_TIMEOUT = 1200  # 秒；1.5 小時直播實測約 1～3 分鐘
MAX_ATTEMPTS = 3
VERSION = 4  # 2：加 audio_peaks、mp4_offset_sec、highlights[].source；3：加 candidates（分段列全部候選）；4：4～5 星候選加 related（2026-11-26）
CHUNK_SEC = 600  # 候選分段掃描：每段幾秒
CHUNK_PAD = 60   # 每段前後多給幾秒上下文
CHUNK_WORKERS = 3
# 峰值基準＝前後 PEAK_WINDOW 秒的第 80 百分位（≈ 平常講話音量）。用中位數不行：0.5 秒一格時中位數落在句子間的
# 靜音，每句話都會高出 20 dB 以上（2026-11-26 用 11-22 Magenta 直播實測）
PEAK_DB = 6.0
PEAK_PCT = 0.8
PEAK_WINDOW = 30
PEAK_TOP = 20
MAX_OFFSET = 300    # 字幕比 mp4 晚最多幾秒（對時搜尋範圍）
KST = timezone(timedelta(hours=9))
TIME_NOTE = ("時間照原直播算（跟 ko_KR.vtt 一致）；回放 mp4 開頭的等待畫面被剪掉，通常比這裡早 2～3 分鐘，"
             "剪之前用 video_toolkit `translate.py transcript` 對時（它會算 offset）")

_running: dict[str, tuple[subprocess.Popen, Path]] = {}  # uid → (行程, log 檔)


def enabled() -> bool:
    return (bool(os.environ.get("LIVE_ARCHIVE_DIR")) and bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
            and os.environ.get("LIVE_SUMMARY", "1") != "0")


def init_db(db: sqlite3.Connection) -> None:
    # status：pending 等著跑、done 好了、failed 放棄
    db.execute("CREATE TABLE IF NOT EXISTS live_summary (uid TEXT PRIMARY KEY, name TEXT, status TEXT, "
               "attempts INTEGER DEFAULT 0, updated INTEGER)")


def enqueue(db: sqlite3.Connection, uid: str, name: str) -> None:
    """live_archive 存好一場後呼叫。"""
    if enabled():
        db.execute("INSERT OR IGNORE INTO live_summary (uid, name, status, updated) VALUES (?,?,?,?)",
                   (uid, name, "pending", int(time.time())))


def step(db: sqlite3.Connection) -> None:
    """每輪呼叫：收跑完的、有待做的就開一個背景子行程。"""
    if not enabled():
        return
    from notify import send_admin

    folder = Path(os.environ["LIVE_ARCHIVE_DIR"])
    now = int(time.time())
    for uid, (proc, log) in list(_running.items()):
        if proc.poll() is None:
            continue
        del _running[uid]
        out = log.read_text(errors="replace") if log.exists() else ""
        log.unlink(missing_ok=True)
        name, attempts = db.execute("SELECT name, attempts FROM live_summary WHERE uid=?", (uid,)).fetchone()
        if proc.returncode == 0:
            db.execute("UPDATE live_summary SET status='done', updated=? WHERE uid=?", (now, uid))
            logging.info("直播統整完成 %s", name)
            send_picks(folder / f"{name}.summary.json", [f"NAS media/weverse_live/{name}"])
        else:
            tail = (out.strip().splitlines() or ["(沒有輸出)"])[-1]
            logging.warning("直播統整失敗 %s（第 %d 次）：%s", name, attempts, tail)
            if attempts >= MAX_ATTEMPTS:
                db.execute("UPDATE live_summary SET status='failed', updated=? WHERE uid=?", (now, uid))
                try:
                    send_admin(f"⚠️ 直播統整放棄（試了 {attempts} 次）：{name}\n{tail[:300]}")
                except Exception as e:
                    logging.warning("統整失敗通知沒送出：%s", e)
        db.commit()

    if _running:
        return
    row = db.execute("SELECT uid, name, attempts FROM live_summary WHERE status='pending' ORDER BY updated").fetchone()
    if not row:
        return
    uid, name, attempts = row
    db.execute("UPDATE live_summary SET attempts=?, updated=? WHERE uid=?", (attempts + 1, now, uid))
    db.commit()
    logging.info("開始直播統整 %s", name)
    log = folder / f".{uid[4:]}.summary.log"
    with log.open("w") as f:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", name],
                                stdout=f, stderr=subprocess.STDOUT, umask=0)
    _running[uid] = (proc, log)


def send_picks(path: Path, footer: list[str]) -> bool:
    """統整完私訊兩則：整場統整＋大綱、爆點列表（使用者回編號挑；2026-11-26 起不用回 Mac 跑 picks）。"""
    import picks_msg
    from notify import send_admin_html
    try:
        s = json.loads(path.read_text(encoding="utf-8"))
        for text in picks_msg.messages(s, footer=footer):
            send_admin_html(text)
        return True
    except Exception as e:
        logging.warning("大綱＋爆點列表沒送出 %s：%s", path.name, e)
        return False


# ───── 產統整（子行程裡跑） ─────

def _ts(sec: float) -> str:
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _sec(ts: str) -> int:
    parts = [int(p) for p in ts.strip().split(":")]
    if len(parts) == 2:
        parts.insert(0, 0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


_CUE = re.compile(r"(\d+):(\d\d):(\d\d)[.,]\d+\s+-->")


def read_vtt(path: Path) -> list[tuple[int, str]]:
    """回傳 [(秒, 句子)]；去掉標籤、空句和連續重複。"""
    lines: list[tuple[int, str]] = []
    t = None
    buf: list[str] = []

    def flush():
        text = re.sub(r"<[^>]+>", "", " ".join(buf)).strip()
        if t is not None and text and not (lines and lines[-1][1] == text):
            lines.append((t, text))

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _CUE.match(raw)
        if m:
            flush()
            buf = []
            t = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
        elif raw.strip() and not raw.strip().isdigit() and t is not None:
            buf.append(raw.strip())
        elif not raw.strip():
            flush()
            buf = []
    flush()
    return lines


# ───── 聲音峰值（補字幕抓不到的畫面／反應笑點） ─────

def loudness(mp4: Path) -> list[float]:
    """每 0.5 秒一個 RMS dB（ffmpeg astats）。"""
    af = ("aresample=8000,asetnsamples=n=4000:p=0,astats=metadata=1:reset=1,"
          "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-")
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(mp4), "-vn", "-ac", "1", "-af", af, "-f", "null", "-"],
                       capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 失敗：{r.stderr.strip()[-300:]}")
    out = []
    for line in r.stdout.splitlines():
        if line.startswith("lavfi."):
            v = line.split("=", 1)[1].strip()
            try:
                out.append(max(-100.0, float(v)))
            except ValueError:
                out.append(-100.0)  # -inf＝完全無聲
    return out


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[len(xs) // 2]


def estimate_offset(levels: list[float], lines: list[tuple[int, str]]) -> tuple[int, float]:
    """字幕時間 − mp4 時間（秒），用「有聲音」跟「有字幕」的重疊找最像的位移。回傳 (offset, z 分數)。"""
    n = len(levels) // 2
    env = [max(levels[2 * i], levels[2 * i + 1]) for i in range(n)]
    med = _median(env)
    act = [1.0 if e > med + 6 else 0.0 for e in env]
    dur = n + MAX_OFFSET + 10
    cov = [0.0] * dur
    for t, _ in lines:
        for k in range(t, min(dur, t + 3)):
            cov[k] = 1.0
    ma, mc = sum(act) / n, sum(cov) / dur
    scores = []
    for lag in range(MAX_OFFSET + 1):
        scores.append(sum((act[i] - ma) * (cov[i + lag] - mc) for i in range(n)))
    mean = sum(scores) / len(scores)
    sd = (sum((x - mean) ** 2 for x in scores) / len(scores)) ** 0.5 or 1.0
    best = max(range(len(scores)), key=scores.__getitem__)
    return best, round((scores[best] - mean) / sd, 1)


def find_peaks(levels: list[float]) -> list[dict]:
    """比局部講話音量（前後 PEAK_WINDOW 秒的 PEAK_PCT 百分位）高 PEAK_DB 以上的段落（mp4 時間），
    相鄰 2 秒內合併，取最大的 PEAK_TOP 段。"""
    half = int(PEAK_WINDOW / 0.5)
    floor = _median(levels) + 3  # 底噪裡的小起伏不算
    hits = []
    for i, v in enumerate(levels):
        if v < floor:
            continue
        w = sorted(levels[max(0, i - half): i + half + 1])
        base = w[int(len(w) * PEAK_PCT)]
        if v - base >= PEAK_DB:
            hits.append((i * 0.5, round(v - base, 1)))
    segs: list[dict] = []
    for t, over in hits:
        if segs and t - segs[-1]["end"] <= 2:
            segs[-1]["end"] = t + 0.5
            segs[-1]["db_over"] = max(segs[-1]["db_over"], over)
        else:
            segs.append({"start": t, "end": t + 0.5, "db_over": over})
    segs = sorted(segs, key=lambda x: -x["db_over"])[:PEAK_TOP]
    return sorted(segs, key=lambda x: x["start"])


def audio_peaks(mp4: Path, lines: list[tuple[int, str]]) -> tuple[list[dict], int | None, float | None, str]:
    """回傳 (peaks（字幕時間＋mp4 時間）, offset, z, 說明)。沒影片或 ffmpeg 失敗就回空清單。"""
    if not mp4.exists():
        return [], None, None, "沒有 mp4，未分析聲音"
    try:
        levels = loudness(mp4)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
        return [], None, None, f"聲音分析失敗：{e}"
    if len(levels) < 120:
        return [], None, None, "影片太短，未分析聲音"
    offset, z = estimate_offset(levels, lines)
    if z < 3:  # 對時不可靠：當作 0，並寫明
        note = f"對時不可靠（z={z}），offset 當 0；剪之前用 transcript 對時"
        offset_used = 0
    else:
        note = f"自動對時 offset={offset} 秒（z={z}）"
        offset_used = offset
    peaks = []
    for p in find_peaks(levels):
        a, b = int(p["start"]) + offset_used, int(p["end"] + 0.999) + offset_used
        text = " ".join(s for t, s in lines if a - 2 <= t <= b + 1) or None
        peaks.append({"start": a, "end": b, "start_ts": _ts(a), "end_ts": _ts(b),
                      "mp4_start": round(p["start"], 1), "mp4_end": round(p["end"], 1),
                      "db_over": p["db_over"], "has_sub": text is not None, "text": text})
    return peaks, offset if z >= 3 else None, z, note


def n_highlights(duration: float) -> int:
    """每 6 分鐘 1 個、3～12 個（使用者 2026-11-26 回測後改；原本每 12 分鐘 1 個、3～8 個，25 分鐘直播只給 3 個會漏掉做過的段）。"""
    return max(3, min(12, round(duration / 60 / 6)))


_TS = {"type": "string", "pattern": r"^(\d+:)?\d{1,2}:\d{2}$"}
_PART = {"type": "object", "additionalProperties": False, "required": ["start", "end", "zh"],
         "properties": {"start": _TS, "end": _TS, "zh": {"type": "string"}}}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title_zh", "summary", "segments", "highlights"],
    "properties": {
        "title_zh": {"type": "string"},
        "summary": {"type": "string"},
        "segments": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["start", "end", "topic"],
            "properties": {"start": _TS, "end": _TS, "topic": {"type": "string"}}}},
        "highlights": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["title", "score", "setup", "payoff", "after", "why", "quotes", "members_mentioned", "fit", "source"],
            "properties": {
                "title": {"type": "string"},
                "score": {"type": "integer", "minimum": 1, "maximum": 5},
                "setup": _PART, "payoff": _PART,
                "after": {"anyOf": [_PART, {"type": "null"}]},
                "why": {"type": "string"},
                "quotes": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["t", "ko", "zh"],
                    "properties": {"t": _TS, "ko": {"type": "string"}, "zh": {"type": "string"}}}},
                "members_mentioned": {"type": "array", "items": {"type": "string"}},
                "fit": {"type": "string", "enum": ["short", "long"]},
                "source": {"type": "string", "enum": ["sub", "audio", "both"]},
            }}},
    },
}


def artist_name() -> str:
    """config.json 第一個 artist（模板／其他團體共用這份程式）。"""
    try:
        return json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))["artists"][0]["name"]
    except (OSError, ValueError, KeyError, IndexError):
        return "K-pop"


def name_hint() -> str:
    """名字寫法的例子；只有 QWER 有整理好的。"""
    if artist_name() == "QWER":
        return "成員本名也算本人（例：娜榮＝Hina）；連名帶姓（例：이시연）也一律寫英文藝名（Siyeon），不寫中文姓名。"
    return "成員本名、連名帶姓也一律寫詞彙表的藝名。"


def system_prompt(n: int) -> str:
    glossary = json.loads((Path(__file__).parent / "glossary.json").read_text(encoding="utf-8"))
    names = "、".join(f"{k} → {v}" for k, v in glossary.items())
    return (
        f"你是 K-pop 直播精華企劃，幫台灣的翻譯剪輯團隊（{artist_name()} 粉絲頻道）整理成員的 Weverse 直播。\n"
        "輸入是整場直播的韓文自動字幕，每行「時間 句子」，時間是直播中的時間點。\n"
        "全部用台灣繁體中文寫，語氣自然。\n\n"
        "要產出：\n"
        "1. title_zh：直播標題的中文翻譯（emoji 保留）。\n"
        "2. summary：整場統整 3～6 句：在哪裡、做什麼、聊了哪些主題、氣氛、有沒有重要消息（行程、新歌、近況、對其他成員的爆料）。\n"
        "3. segments：照話題切段，從頭到尾覆蓋整場，每段一句話說聊什麼。\n"
        f"4. highlights：挑 {n} 個最值得剪成中字精華的片段（素材真的不夠可以少，但至少 1 個），照精彩程度由高到低排。\n"
        "   - 每個都要有頭有尾：setup＝前因（怎麼聊到的、鋪陳），payoff＝爆點本身，after＝後續反應或收尾（沒有就 null）。"
        "觀眾只看這段也要看得懂。\n"
        "   - 時間一律用輸入裡真的有的時間點，不要編。setup 通常 10～90 秒。\n"
        "   - fit：setup 開頭到結尾 60 秒內講得完＝short（適合 Shorts），需要更長＝long。\n"
        "   - why：用粉絲的角度說為什麼好笑、可愛、好哭或有料。\n"
        "   - quotes：2～5 句關鍵原話；ko 照字幕原文，zh 照字直翻成台灣口語，不加油添醋、不裝可愛。\n"
        "   - members_mentioned：這段提到的其他成員（不含直播的人）；沒有就空陣列。\n"
        "   - source：主要靠字幕內容挑的＝sub；靠聲音峰值挑的＝audio；兩者都有＝both。\n"
        "   - score：1～5，5＝一定要剪。\n\n"
        "注意：\n"
        "- 這是自動字幕，常聽錯字、斷句怪、前幾分鐘可能空白。照上下文理解；quotes 的 ko 保留字幕原樣，"
        "覺得字幕聽錯的在 why 註明「字幕可能聽錯」。\n"
        "- 你只看得到字幕、看不到畫面：純畫面的笑點不要猜。\n"
        "- 字幕後面附「聲音峰值」：音量比前後 30 秒平常講話突然高出很多的時間點（大笑、尖叫、驚呼、拍桌等），附那幾秒的字幕。"
        "無字幕或字幕平淡、但聲音很大的段落，可能是動作或反應笑點，也要考慮列進 highlights（用峰值前後的字幕推前因後果），"
        "why 註明「聲音峰值」並寫高出幾 dB；不確定是什麼就照實說「聲音很大，要看畫面確認」。音樂、雜音造成的峰值不要列。\n"
        "- 只用字幕裡有的內容，不要用你自己知道的背景知識補劇情。\n"
        f"- 名字照詞彙表：{names}。{name_hint()}\n"
        + taste.prompt_block()
    )


def build(name: str, folder: Path) -> dict:
    info_path = folder / f"{name}.info.json"
    vtt = folder / f"{name}.ko_KR.vtt"
    if not vtt.exists():
        raise SystemExit(f"沒有韓文字幕：{vtt.name}")
    info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {}
    lines = read_vtt(vtt)
    if len(lines) < 5:
        raise SystemExit(f"字幕太少（{len(lines)} 句），不統整")
    duration = float(info.get("duration") or lines[-1][0])
    m = re.match(r"(\d{8})_(.+?)_", name)
    member = m[2] if m else ""
    ts = info.get("release_timestamp") or info.get("timestamp")
    date = datetime.fromtimestamp(ts, KST).strftime("%Y-%m-%d") if ts else (
        f"{m[1][:4]}-{m[1][4:6]}-{m[1][6:]}" if m else "")
    n = n_highlights(duration)

    header = (f"直播的人：{member}（{artist_name()}＝團體直播）\n標題：{info.get('title', '')}\n"
              f"日期（韓國時間）：{date}\n長度：{_ts(duration)}\n\n字幕：\n")
    body = "\n".join(f"{_ts(t)} {s}" for t, s in lines)
    peaks, offset, z, audio_note = audio_peaks(folder / f"{name}.mp4", lines)
    print(audio_note, f"，聲音峰值 {len(peaks)} 段")
    if peaks:
        body += "\n\n聲音峰值（時間跟字幕同一條時間軸；+dB＝比前後 30 秒平常講話音量高出多少）：\n" + "\n".join(
            f"{p['start_ts']}–{p['end_ts']} +{p['db_over']}dB：{p['text'] or '無字幕'}" for p in peaks)
    data = ask(system_prompt(n), header + body)
    cands = scan_candidates(header.rsplit("\n\n字幕：", 1)[0], lines, peaks, "Weverse 直播")

    def conv(part):
        if part is None:
            return None
        return {"start": _sec(part["start"]), "end": _sec(part["end"]),
                "start_ts": part["start"], "end_ts": part["end"], "zh": part["zh"]}

    highlights = []
    for i, h in enumerate(data["highlights"], 1):
        parts = [p for p in (h["setup"], h["payoff"], h["after"]) if p]
        highlights.append({
            "id": i, "title": h["title"], "score": h["score"], "fit": h["fit"],
            "start": min(_sec(p["start"]) for p in parts), "end": max(_sec(p["end"]) for p in parts),
            "setup": conv(h["setup"]), "payoff": conv(h["payoff"]), "after": conv(h["after"]),
            "why": h["why"],
            "quotes": [{"t": _sec(q["t"]), "t_ts": q["t"], "ko": q["ko"], "zh": q["zh"]} for q in h["quotes"]],
            "members_mentioned": h["members_mentioned"], "source": h["source"],
        })
    return {
        "version": VERSION,
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "model": MODEL,
        "live": {
            "name": name, "post_id": info.get("id") or name.rsplit("_", 1)[-1], "member": member,
            "title_ko": info.get("title", ""), "title_zh": data["title_zh"], "date": date,
            "duration_sec": int(duration), "url": info.get("webpage_url", ""), "thumbnail": info.get("thumbnail", ""),
            "files": {"mp4": f"{name}.mp4", "ko_vtt": vtt.name, "zh_vtt": f"{name}.zh_TW.vtt", "info": info_path.name},
        },
        "time_base": "vtt",
        "mp4_offset_sec": offset,
        "offset_z": z,
        "audio_note": audio_note,
        "time_note": TIME_NOTE,
        "summary": data["summary"],
        "segments": [{"start": _sec(s["start"]), "end": _sec(s["end"]), "start_ts": s["start"], "end_ts": s["end"],
                      "topic": s["topic"]} for s in data["segments"]],
        "highlights": highlights,
        "candidates": cands,
        "audio_peaks": peaks,
    }


def ask(system: str, text: str, schema: dict | None = None) -> dict:
    """claude -p＋--json-schema：不給工具、不讀設定檔和 MCP、不存 session。走訂閱（拿掉 ANTHROPIC_API_KEY）。"""
    cmd = [
        "claude", "-p", "--model", MODEL, "--system-prompt", system,
        "--tools", "", "--strict-mcp-config", "--setting-sources", "",
        "--disable-slash-commands", "--no-session-persistence",
        "--output-format", "json", "--json-schema", json.dumps(schema or SCHEMA),
    ]
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=CLI_TIMEOUT, env=env)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise SystemExit(f"claude 輸出不是 JSON（exit {proc.returncode}）：{(proc.stderr or proc.stdout)[:300]}") from None
    if proc.returncode != 0 or data.get("is_error"):
        raise SystemExit(f"claude 回報錯誤：{str(data.get('result'))[:300]}")
    out = data.get("structured_output")
    if not isinstance(out, dict):
        raise SystemExit(f"沒拿到結構化輸出（stop_reason={data.get('stop_reason')}）")
    print(f"claude 完成：{data.get('duration_ms', 0) / 1000:.0f} 秒，牌價換算 ${data.get('total_cost_usd', 0):.3f}（訂閱不扣）")
    return out


CAND_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["candidates"],
    "properties": {"candidates": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["start", "end", "payoff", "title", "score", "why", "quotes", "fit", "source"],
        "properties": {
            "start": _TS, "end": _TS, "payoff": _TS,
            "title": {"type": "string"},
            "score": {"type": "integer", "minimum": 1, "maximum": 5},
            "why": {"type": "string"},
            "quotes": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["t", "ko", "zh"],
                "properties": {"t": _TS, "ko": {"type": "string"}, "zh": {"type": "string"}}}},
            "fit": {"type": "string", "enum": ["short", "long"]},
            "source": {"type": "string", "enum": ["sub", "audio", "both"]},
        }}}},
}


def candidates_prompt(kind: str) -> str:
    glossary = json.loads((Path(__file__).parent / "glossary.json").read_text(encoding="utf-8"))
    names = "、".join(f"{k} → {v}" for k, v in glossary.items())
    return (
        f"你是 K-pop 精華企劃，幫台灣的翻譯剪輯團隊（{artist_name()} 粉絲頻道）從{kind}裡找可以剪成中字精華的片段。\n"
        "輸入是其中一段的韓文字幕（每行「時間 句子」），可能附這段的聲音峰值。標「前文」「後文」的只是上下文。\n"
        "任務：把『這段』裡所有可能剪得出來的時刻全部列出來（candidates）——好笑、可愛、反轉、吐槽、爆料、反應大、成員互動、"
        "台灣相關、感人、重要消息都算。**寧可多列、不能漏**：使用者會自己篩，漏掉比多列嚴重。每 10 分鐘通常有 5～15 個，"
        "真的平淡可以少，但不要只挑最好的幾個。\n"
        "每個候選：start／end＝前因開頭到後續結束（觀眾只看這段也看得懂），payoff＝爆點那句的時間；title 一句話；"
        "score 1～5（5＝一定要剪、1＝勉強可用，低分的也要列）；why 一句說為什麼可以剪（字幕可能聽錯、聲音峰值要看畫面確認的照實寫）；"
        "quotes 1～3 句關鍵原話（ko 照字幕原文、zh 照字直翻台灣口語）；fit：60 秒內講得完＝short，否則 long；"
        "source：靠字幕＝sub、靠聲音峰值＝audio、兩者＝both。\n"
        "規則：時間一律用輸入裡真的有的時間點；只用字幕內容，不用背景知識補劇情；看不到畫面，純畫面笑點不要猜，"
        "但字幕平淡、聲音很大的地方要列（why 註明要看畫面）。音樂、音效造成的峰值不列。台灣繁體中文。\n"
        f"- 名字照詞彙表：{names}。{name_hint()}\n"
        + taste.prompt_block().replace("挑 highlights 和給 score 時參考，但素材本身好笑還是要挑",
                                       "**只影響 score 高低，不影響要不要列**：不合口味的也照列、給低分")
    )


def scan_candidates(header: str, lines: list[tuple[int, str]], peaks: list[dict], kind: str) -> list[dict]:
    """每 CHUNK_SEC 秒一段（前後多給 CHUNK_PAD 秒上下文）各自列全部候選，合併去重、照時間排。"""
    from concurrent.futures import ThreadPoolExecutor
    if not lines:
        return []
    end = lines[-1][0] + 1
    system = candidates_prompt(kind)

    def one(a: int) -> list[dict]:
        b = a + CHUNK_SEC
        ctx = [f"{'前文 ' if t < a else '後文 ' if t >= b else ''}{_ts(t)} {s}" for t, s in lines
               if a - CHUNK_PAD <= t < b + CHUNK_PAD]
        if not any(a <= t < b for t, _ in lines):
            return []
        body = header + f"\n這段：{_ts(a)}～{_ts(min(b, end))}\n\n字幕：\n" + "\n".join(ctx)
        pk = [p for p in peaks if a <= p["start"] < b]
        if pk:
            body += "\n\n聲音峰值（+dB＝比前後 30 秒平常音量高出多少）：\n" + "\n".join(
                f"{p['start_ts']}–{p['end_ts']} +{p['db_over']}dB：{p['text'] or '無字幕'}" for p in pk)
        out = []
        for c in ask(system, body, CAND_SCHEMA)["candidates"]:
            st, en, po = _sec(c["start"]), _sec(c["end"]), _sec(c["payoff"])
            if not a <= po < b:
                continue  # 爆點在上下文裡的交給隔壁段
            out.append({"start": min(st, po), "end": max(en, po), "payoff": po, "start_ts": _ts(min(st, po)),
                        "end_ts": _ts(max(en, po)), "payoff_ts": c["payoff"], "title": c["title"], "score": c["score"],
                        "why": c["why"], "fit": c["fit"], "source": c["source"],
                        "quotes": [{"t": _sec(q["t"]), "t_ts": q["t"], "ko": q["ko"], "zh": q["zh"]} for q in c["quotes"]]})
        return out

    starts = list(range(0, end, CHUNK_SEC))
    with ThreadPoolExecutor(CHUNK_WORKERS) as ex:
        found = [c for part in ex.map(one, starts) for c in part]
    found.sort(key=lambda c: c["start"])
    merged: list[dict] = []
    for c in found:  # 重疊超過一半當同一段，留分數高的
        dup = next((m for m in merged if min(m["end"], c["end"]) - max(m["start"], c["start"])
                    > 0.5 * min(m["end"] - m["start"] or 1, c["end"] - c["start"] or 1)), None)
        if dup is None:
            merged.append(c)
        elif c["score"] > dup["score"]:
            merged[merged.index(dup)] = c
    for i, c in enumerate(merged, 1):
        c["id"] = i
    group_candidates(merged)
    print(f"候選 {len(merged)} 個（{len(starts)} 段）")
    return merged


GROUP_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["groups"],
    "properties": {"groups": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["top", "related"],
        "properties": {"top": {"type": "integer"}, "related": {"type": "array", "items": {"type": "integer"}}}}}},
}


def group_candidates(cands: list[dict]) -> None:
    """4～5 星候選加 related：1～3 星裡跟它同一件事的（前因／後續／同一個梗），每個低星只掛一個。就地改；失敗就不掛。"""
    tops = {c["id"] for c in cands if c["score"] >= 4}
    if not tops or len(tops) == len(cands):
        return
    text = "\n".join(f"{c['id']}\t{c['start_ts']}–{c['end_ts']}\t★{c['score']}\t{c['title']}｜{c['why'][:100]}" for c in cands)
    system = ("輸入是一支影片的爆點候選（id、時間、星數、標題｜原因）。對每個 4～5 星的候選，找出 1～3 星裡跟它是同一件事的："
              "它的前因（鋪陳、怎麼聊到的）、後續（反應、收尾）、或同一個梗的延伸。只掛真的有關的，時間相近但話題不同的不要掛；"
              "每個低星最多掛在一個 4～5 星底下（掛最相關的）。沒有就給空陣列。")
    try:
        groups = ask(system, text, GROUP_SCHEMA)["groups"]
    except SystemExit as e:
        print(f"候選分組失敗，不掛：{e}")
        return
    low = {c["id"] for c in cands if c["score"] < 4}
    taken: set[int] = set()
    rel = {}
    for g in groups:
        if g["top"] in tops:
            ids = [i for i in g["related"] if i in low and i not in taken]
            taken.update(ids)
            rel[g["top"]] = ids
    for c in cands:
        if c["score"] >= 4:
            c["related"] = rel.get(c["id"], [])
    print(f"候選分組：{len(taken)} 個低星掛到 4～5 星底下")


def candidates_markdown(cands: list[dict]) -> list[str]:
    out = ["", f"## 全部候選（照時間，{len(cands)} 個；星數＝建議度，低星也列出來給你篩）", ""]
    for c in cands:
        out.append(f"- `{c['start_ts']}–{c['end_ts']}` {'★' * c['score']} **{c['title']}**"
                   f"（{'Shorts' if c['fit'] == 'short' else '長片'}）— {c['why']}")
        out += [f"  - `{q['t_ts']}` {q['ko']} → {q['zh']}" for q in c["quotes"]]
    return out


def to_markdown(s: dict) -> str:
    lv = s["live"]
    out = [f"# {lv['date']} {lv['member']} 直播：{lv['title_zh']}", "",
           f"- 原標題：{lv['title_ko']}", f"- 長度：{_ts(lv['duration_sec'])}", f"- 連結：{lv['url']}",
           f"- 時間照原直播算，回放檔通常早 2～3 分鐘", "", "## 統整", "", s["summary"], "", "## 段落", ""]
    out += [f"- `{x['start_ts']}–{x['end_ts']}` {x['topic']}" for x in s["segments"]]
    out += ["", "## 精選爆點", ""]
    for h in s["highlights"]:
        out += [f"### {h['id']}. {h['title']}（{'★' * h['score']}，{'Shorts' if h['fit'] == 'short' else '長片'}）",
                f"`{_ts(h['start'])}–{_ts(h['end'])}`", ""]
        for label, key in (("前因", "setup"), ("爆點", "payoff"), ("後續", "after")):
            p = h[key]
            if p:
                out.append(f"- **{label}** `{p['start_ts']}–{p['end_ts']}`：{p['zh']}")
        out += [f"- **為什麼**：{h['why']}"]
        if h["members_mentioned"]:
            out.append(f"- **提到**：{'、'.join(h['members_mentioned'])}")
        out += [f"  - `{q['t_ts']}` {q['ko']} → {q['zh']}" for q in h["quotes"]]
        out.append("")
    return "\n".join(out + candidates_markdown(s.get("candidates", [])))


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o666)  # 容器是 root；讓 NAS 帳號也能刪改
    os.replace(tmp, path)


def run(name: str, folder: Path | None = None) -> Path:
    folder = folder or Path(os.environ["LIVE_ARCHIVE_DIR"])
    s = build(name, folder)
    _write(folder / f"{name}.summary.json", json.dumps(s, ensure_ascii=False, indent=2))
    _write(folder / f"{name}.summary.md", to_markdown(s))
    print(f"寫好 {name}.summary.json／.md：{len(s['segments'])} 段、{len(s['highlights'])} 個爆點")
    return folder / f"{name}.summary.json"


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "run":
        run(sys.argv[2], Path(sys.argv[3]) if len(sys.argv) > 3 else None)
    else:
        sys.exit('用法：python live_summary.py run "<存檔檔名（不含副檔名）>" [資料夾]')
