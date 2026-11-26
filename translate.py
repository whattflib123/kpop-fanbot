"""韓文／英文標題、貼文翻成繁體中文。

兩種後端（同一份 prompt 和詞彙表）：
- cli：呼叫 Claude Code（`claude -p`），用訂閱額度，不花 API 費。設了 CLAUDE_CODE_OAUTH_TOKEN 就走這條。
- api：Claude API（ANTHROPIC_API_KEY，按 token 計費）。cli 失敗且有設 key 時也會退回這條。
TRANSLATE_BACKEND=cli／api 可以強制指定。
"""
import json
import logging
import os
import re
import subprocess
from pathlib import Path

import anthropic

MODEL = "claude-sonnet-5-5"
CLI_TIMEOUT = 180  # 秒；一則通常 2～5 秒
GLOSSARY = json.loads((Path(__file__).parent / "glossary.json").read_text(encoding="utf-8"))

_GLOSSARY_TEXT = "詞彙表（原文 → 譯名）：\n" + "\n".join(f"{k} → {v}" for k, v in GLOSSARY.items())
_COMMON_RULES = (
    "- 團名、成員名照詞彙表；表內沒有的人名保留原文或用常見譯名。\n"
    "- 成員本名照詞彙表連括號一起寫（例：中文本名（英文名）），讓讀者知道是誰。\n"
    "- 保留 emoji、#hashtag、[ ] 等格式符號；歌名可保留原文並在後面括號附中文。\n"
    "- 已經是中文就原樣輸出。\n"
)
SYSTEM = (
    "你是 K-pop 追星翻譯。把使用者給的 YouTube 影片標題翻成台灣繁體中文，語氣自然、像台灣粉絲會寫的標題。\n"
    "規則：\n- 只輸出譯文一行，不加引號、說明或原文。\n" + _COMMON_RULES + _GLOSSARY_TEXT
)
SYSTEM_POST = (
    "你是 K-pop 翻譯。使用者給的是成員在 Weverse／Instagram 寫給粉絲的貼文或留言，翻成台灣繁體中文。\n"
    "規則：\n- 只輸出譯文，忠實不刪減，不加開場、評論、引號或原文。\n"
    "- 保留原本的換行和語氣（ㅋㅋㅋ 可譯成哈哈哈）。\n"
    "- 只用子音寫的初聲猜謎（例如 ㅋㄹㅁㅂㅂ）保留原文，後面加「（韓文子音猜謎）」，不要寫出答案；"
    "ㅋㅋ、ㅎㅎ、ㅠㅠ、ㅜㅜ 這類常見表情不算。\n"
    "- 可愛的語尾或語氣（例如 ~핑、~용）中文表達不出來時，可以加一個符合語氣的 emoji。\n" + _COMMON_RULES + _GLOSSARY_TEXT
)

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def needs_translation(text: str) -> bool:
    """只有 emoji／符號的短文不用翻。"""
    return any(ch.isalpha() for ch in text)


class TranslateError(Exception):
    """翻譯失敗（額度不足、API 錯誤、被拒、截斷）。呼叫端不推播，下輪重試。"""


# 模型偶爾把自我修正混進譯文（例如「Wait, I must not add emoji… Correct output:」），原文沒有這些字才算外洩
_LEAK = re.compile(r"(?i)\b(wait,|correct output|corrected output|i must|i should|let me|my mistake|"
                   r"actually,|note:|translation:|output:)")


def _leaked(src: str, out: str) -> bool:
    found = {m.group(0).lower() for m in _LEAK.finditer(out)}
    return any(f not in src.lower() for f in found)


def translate(title: str, post: bool = False) -> str | None:
    """回傳譯文；不需要翻（emoji／符號）回傳 None；失敗丟 TranslateError。post=True 用貼文 prompt。
    譯文混進模型的自言自語就重翻一次，還是有就當失敗（下輪重試）。"""
    if not needs_translation(title):
        return None
    for _ in range(2):
        text = _translate_once(title, post)
        if re.search(r"[가-힣]", title) and text.strip() == title.strip():
            logging.warning("模型把韓文原文原樣吐回來，重翻：%r", text[:100])  # 2026-11-26 遇到過
            continue
        if not _leaked(title, text):
            return text
        logging.warning("譯文混進模型自言自語，重翻：%r", text[:200])
    raise TranslateError("譯文混進模型的自言自語或沒翻（重翻仍然有）")


def _backend() -> str:
    return os.environ.get("TRANSLATE_BACKEND") or ("cli" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "api")


def _translate_once(title: str, post: bool) -> str:
    if _backend() == "cli":
        try:
            return _translate_cli(title, post)
        except TranslateError as e:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise
            logging.warning("Claude Code 翻譯失敗，這則改走 API（會計費）：%s", e)
    return _translate_api(title, post)


def _translate_cli(title: str, post: bool) -> str:
    """claude -p：不給工具、不讀設定檔和 MCP、不存 session，只拿一段譯文。"""
    cmd = [
        "claude", "-p", "--model", MODEL, "--effort", "low",
        "--system-prompt", SYSTEM_POST if post else SYSTEM,
        "--tools", "", "--strict-mcp-config", "--setting-sources", "",
        "--disable-slash-commands", "--no-session-persistence", "--output-format", "json",
    ]
    # 環境裡有 ANTHROPIC_API_KEY 的話 CLI 會改用它計費，要拿掉才會走訂閱
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        proc = subprocess.run(cmd, input=title, capture_output=True, text=True, timeout=CLI_TIMEOUT, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TranslateError(f"claude 指令跑不起來或逾時：{e}") from e
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise TranslateError(f"claude 輸出不是 JSON（exit {proc.returncode}）：{(proc.stderr or proc.stdout)[:300]}") from None
    if proc.returncode != 0 or data.get("is_error"):
        raise TranslateError(f"claude 回報錯誤：{str(data.get('result'))[:300]}")
    if data.get("stop_reason") == "refusal":
        raise TranslateError("Claude 拒絕翻譯")
    if data.get("stop_reason") == "max_tokens":
        raise TranslateError("譯文被截斷（超過 max_tokens）")
    text = (data.get("result") or "").strip()
    if not text:
        raise TranslateError("譯文是空的")
    return text


def _translate_api(title: str, post: bool) -> str:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise TranslateError("未設定 ANTHROPIC_API_KEY")
    try:
        resp = _get_client().beta.messages.create(
            model=MODEL,
            max_tokens=8192,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM_POST if post else SYSTEM,
            messages=[{"role": "user", "content": title}],
        )
    except Exception as e:
        raise TranslateError(str(e)) from e
    if resp.stop_reason == "refusal":
        raise TranslateError("Claude 拒絕翻譯")
    if resp.stop_reason == "max_tokens":
        raise TranslateError("譯文被截斷（超過 max_tokens）")
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not text:
        raise TranslateError("譯文是空的")
    return text
