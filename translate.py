"""韓文／英文標題、貼文翻成繁體中文（Claude API）。"""
import json
import logging
import os
from pathlib import Path

import anthropic

MODEL = "claude-sonnet-5-5"
GLOSSARY = json.loads((Path(__file__).parent / "glossary.json").read_text(encoding="utf-8"))

_GLOSSARY_TEXT = "詞彙表（原文 → 譯名）：\n" + "\n".join(f"{k} → {v}" for k, v in GLOSSARY.items())
_COMMON_RULES = (
    "- 團名、成員名照詞彙表；表內沒有的人名保留原文或用常見譯名。\n"
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
    "ㅋㅋ、ㅎㅎ、ㅠㅠ、ㅜㅜ 這類常見表情不算。\n" + _COMMON_RULES + _GLOSSARY_TEXT
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


def translate(title: str, post: bool = False) -> str | None:
    """回傳譯文；不需要翻（emoji／符號）回傳 None；失敗丟 TranslateError。post=True 用貼文 prompt。"""
    if not needs_translation(title):
        return None
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
