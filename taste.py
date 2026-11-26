"""使用者口味：把 video_toolkit 的自評分數（data/scores/video_toolkit.csv）整理成「挑爆點的偏好」，
餵給 live_summary／yt_archive 的 prompt，讓 highlights 的排序和 score 貼近使用者實際給分（2026-11-26）。

- 分數 CSV 比 data/taste.json 新就重新整理（一次 `claude -p`，只在統整子行程裡跑，不卡主迴圈）；失敗沿用舊的。
- 只整理「挑什麼片段」的內容偏好，剪法（特效、字卡、節奏）不算。
- 手動重整：docker exec qwer-update python taste.py refresh；看目前內容：python taste.py show
"""
import csv
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
SCORES = ROOT / "data" / "scores" / "video_toolkit.csv"
OUT = ROOT / "data" / "taste.json"
LIBRARY = ROOT / "data" / "ig_library"
MIN_ROWS = 5

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["likes", "dislikes", "shape"],
    "properties": {
        "likes": {"type": "array", "items": {"type": "string"}},
        "dislikes": {"type": "array", "items": {"type": "string"}},
        "shape": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = (
    "你在幫 K-pop 粉絲翻譯剪輯頻道整理「使用者喜歡什麼樣的片段」。輸入是做好的影片清單：編號、片名、使用者給的分數（1～5）、"
    "使用者原話和剪輯者的解讀。\n"
    "只整理『挑哪段素材』的內容偏好，給之後自動挑直播／綜藝爆點的 AI 看；剪法（特效、字卡、花字、轉場、配樂、版面、改了幾版）一律不要寫。\n"
    "用台灣繁體中文，每條一句、具體（舉片名當例子），不要空話：\n"
    "- likes：高分（4.5～5）片段的題材共通點，最多 6 條。\n"
    "- dislikes：低分（≤3.5）或扣分原因裡跟素材有關的，最多 4 條。\n"
    "- shape：好片段的結構和長度（例：一個爆點＋前因後果、幾秒），最多 3 條。\n"
    "同一支評多次以最後一次為準。證據不夠的不要硬寫。"
)


def _titles() -> dict[str, str]:
    if not LIBRARY.is_dir():
        return {}
    out = {}
    for d in LIBRARY.iterdir():
        if d.is_dir() and "_" in d.name:
            nn, title = d.name.split("_", 1)
            out[nn] = title
    return out


def _rows() -> list[dict]:
    if not SCORES.exists():
        return []
    latest: dict[str, dict] = {}
    with SCORES.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("nn"):
                latest[r["nn"]] = r  # 重評以最後一筆為準
    return list(latest.values())


def refresh() -> dict | None:
    rows = _rows()
    if len(rows) < MIN_ROWS:
        return None
    titles = _titles()
    text = "\n".join(f"{r['nn']}｜{titles.get(r['nn'], '')}｜{r['score']} 分｜{r.get('note', '')}" for r in rows)
    cmd = ["claude", "-p", "--model", "claude-opus-5-5", "--system-prompt", SYSTEM,
           "--tools", "", "--strict-mcp-config", "--setting-sources", "",
           "--disable-slash-commands", "--no-session-persistence",
           "--output-format", "json", "--json-schema", json.dumps(SCHEMA)]
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = subprocess.run(cmd, input=text, capture_output=True, text=True, timeout=600, env=env)
    try:
        data = json.loads(proc.stdout)
        out = data["structured_output"]
        assert proc.returncode == 0 and not data.get("is_error") and isinstance(out, dict)
    except Exception:
        print(f"口味整理失敗，沿用舊的：{(proc.stderr or proc.stdout)[:300]}")
        return None
    out = {**out, "rows": len(rows), "generated_at": datetime.now().isoformat(timespec="seconds")}
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, OUT)
    print(f"口味整理好了（{len(rows)} 支評分）")
    return out


def load() -> dict | None:
    """分數比整理結果新就先重整；失敗或沒資料回舊的或 None。"""
    stale = SCORES.exists() and (not OUT.exists() or SCORES.stat().st_mtime > OUT.stat().st_mtime)
    if stale:
        got = refresh()
        if got:
            return got
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def prompt_block() -> str:
    """接在統整 system prompt 後面；沒資料回空字串。"""
    t = load()
    if not t:
        return ""
    lines = ["\n使用者口味（從使用者過去給剪好影片的分數整理；挑 highlights 和給 score 時參考，但素材本身好笑還是要挑）："]
    lines += [f"- 喜歡：{x}" for x in t.get("likes", [])]
    lines += [f"- 不喜歡：{x}" for x in t.get("dislikes", [])]
    lines += [f"- 結構：{x}" for x in t.get("shape", [])]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "refresh":
        refresh()
        print(prompt_block())
    elif len(sys.argv) == 2 and sys.argv[1] == "show":
        print(prompt_block() or "（還沒有口味資料）")
    else:
        sys.exit("用法：python taste.py refresh｜show")
