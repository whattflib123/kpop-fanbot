"""韓文／英文標題、貼文翻成繁體中文。

兩種後端（同一份 prompt 和詞彙表）：
- cli：呼叫 Claude Code（`claude -p`），用訂閱額度，不花 API 費。設了 CLAUDE_CODE_OAUTH_TOKEN 就走這條。
- api：Claude API（ANTHROPIC_API_KEY，按 token 計費）。cli 失敗且有設 key 時也會退回這條。
TRANSLATE_BACKEND=cli／api 可以強制指定。

翻不出來（韓文原樣吐回兩次，多半是暱稱、合成詞、角色名）時改用 WebSearch 查，丟 NeedsChoice 附候選，
由 choice.py 私訊使用者挑；挑好的譯文放 OVERRIDES，下輪 translate 直接回傳，學到的詞放 LEARNED 併進詞彙表。
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
SEARCH_TIMEOUT = 300  # 秒；上網查一次約 30～60 秒
GLOSSARY = json.loads((Path(__file__).parent / "glossary.json").read_text(encoding="utf-8"))

# choice.load() 每輪從 DB 設定
OVERRIDES: dict[str, str] = {}  # 原文 → 使用者挑定的譯文
PENDING: set[str] = set()       # 已私訊、等使用者挑的原文：不再呼叫模型，直接丟 NeedsChoice
LEARNED: dict[str, str] = {}    # 使用者挑過學到的詞（原文 → 譯名），併進詞彙表

_COMMON_RULES = (
    "- 團名、成員名照詞彙表；表內沒有的人名保留原文或用常見譯名。\n"
    "- 成員本名照詞彙表連括號一起寫（例：中文本名（英文名）），讓讀者知道是誰。\n"
    "- 保留 emoji、#hashtag、[ ] 等格式符號；歌名可保留原文並在後面括號附中文。\n"
    "- 已經是中文就原樣輸出。\n"
)
_TITLE_HEAD = (
    "你是 K-pop 追星翻譯。把使用者給的 YouTube 影片標題翻成台灣繁體中文，語氣自然、像台灣粉絲會寫的標題。\n"
    "規則：\n- 只輸出譯文一行，不加引號、說明或原文。\n"
)
_POST_HEAD = (
    "你是 K-pop 翻譯。使用者給的是成員在 Weverse／Instagram 寫給粉絲的貼文或留言，翻成台灣繁體中文。\n"
    "規則：\n- 只輸出譯文，忠實不刪減，不加開場、評論、引號或原文。\n"
    "- 保留原本的換行和語氣（ㅋㅋㅋ 可譯成哈哈哈）。\n"
    "- 只用子音寫的初聲猜謎（例如 ㅋㄹㅁㅂㅂ）保留原文，後面加「（韓文子音猜謎）」，不要寫出答案；"
    "ㅋㅋ、ㅎㅎ、ㅠㅠ、ㅜㅜ 這類常見表情不算。\n"
    "- 可愛的語尾或語氣（例如 ~핑、~용）中文表達不出來時，可以加一個符合語氣的 emoji。\n"
)
_SEARCH_HEAD = (
    "你是 K-pop 粉絲翻譯研究員。使用者給的韓文是成員寫給粉絲的貼文／留言（或影片標題），一般翻譯翻不出來，"
    "多半是暱稱、合成詞、角色名或迷因。先用 WebSearch 查（原詞、原詞＋團名或成員名），再推測意思。\n"
    "輸出 1～3 個候選，最可能的放第一個；不要給原文照抄的候選（使用者另有「保留原文」按鈕）：\n"
    "- text：整段的完整譯文（台灣繁體中文，可直接發出去；看不懂的詞可寫成「原文（解釋）」）。\n"
    "- why：一句話（60 字內）說根據：查到什麼來源，或怎麼拆字推理；查不到就明說是推測。\n"
    "- terms：你解讀的關鍵詞，term 是原文詞、zh 是放進譯文的寫法。\n"
    "翻譯規則：\n"
)
_SEARCH_SCHEMA = json.dumps({
    "type": "object", "required": ["candidates"],
    "properties": {"candidates": {"type": "array", "minItems": 1, "maxItems": 3, "items": {
        "type": "object", "required": ["text", "why", "terms"],
        "properties": {"text": {"type": "string"}, "why": {"type": "string"},
                       "terms": {"type": "array", "items": {"type": "object", "required": ["term", "zh"],
                                                             "properties": {"term": {"type": "string"},
                                                                            "zh": {"type": "string"}}}}}}}},
})


def _glossary_text() -> str:
    terms = {**LEARNED, **GLOSSARY}  # glossary.json 優先
    return "詞彙表（原文 → 譯名）：\n" + "\n".join(f"{k} → {v}" for k, v in terms.items())


def system_prompt(post: bool) -> str:
    return (_POST_HEAD if post else _TITLE_HEAD) + _COMMON_RULES + _glossary_text()


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


class NeedsChoice(TranslateError):
    """翻不出來，要使用者挑譯法。candidates：[{"text","why","terms":[{"term","zh"}]}]，查不到是空的
    （使用者還是可以選保留原文或自己打）。asked=True 表示已經私訊過、還在等。"""

    def __init__(self, src: str, candidates: list[dict], asked: bool = False, search_error: str | None = None):
        super().__init__("翻不出來，等使用者挑譯法" if asked else "翻不出來，已上網查候選")
        self.src, self.candidates, self.asked, self.search_error = src, candidates, asked, search_error


# 模型偶爾把自我修正混進譯文（例如「Wait, I must not add emoji… Correct output:」），原文沒有這些字才算外洩
_LEAK = re.compile(r"(?i)\b(wait,|correct output|corrected output|i must|i should|let me|my mistake|"
                   r"actually,|note:|translation:|output:)")


def _leaked(src: str, out: str) -> bool:
    found = {m.group(0).lower() for m in _LEAK.finditer(out)}
    return any(f not in src.lower() for f in found)


def translate(title: str, post: bool = False, context: str | None = None) -> str | None:
    """回傳譯文；不需要翻（emoji／符號）回傳 None；失敗丟 TranslateError。post=True 用貼文 prompt。
    譯文混進模型的自言自語就重翻一次，還是有就當失敗（下輪重試）。
    韓文原樣吐回兩次 = 翻不出來 → 上網查候選，丟 NeedsChoice（context：上網查時附的上下文，例如留言所屬貼文）。"""
    if not needs_translation(title):
        return None
    if title in OVERRIDES:
        return OVERRIDES[title]
    if title in PENDING:
        raise NeedsChoice(title, [], asked=True)
    same = 0
    for _ in range(2):
        text = _translate_once(title, post)
        if re.search(r"[가-힣]", title) and text.strip() == title.strip():
            logging.warning("模型把韓文原文原樣吐回來，重翻：%r", text[:100])  # 2026-11-26 遇到過
            same += 1
            continue
        if not _leaked(title, text):
            return text
        logging.warning("譯文混進模型自言自語，重翻：%r", text[:200])
    if same == 2:  # 2026-11-28「시몽가 vs 쿠로밍」：暱稱合成詞，模型照規則保留原文
        try:
            cands = search_candidates(title, post, context)
        except TranslateError as e:
            logging.warning("上網查候選失敗：%s", e)
            raise NeedsChoice(title, [], search_error=str(e)) from None
        raise NeedsChoice(title, cands)
    raise TranslateError("譯文混進模型的自言自語或沒翻（重翻仍然有）")


def search_candidates(title: str, post: bool, context: str | None = None) -> list[dict]:
    """claude -p 開 WebSearch 查，回候選譯法（結構化輸出）。只走訂閱（cli），沒有 API 版。"""
    if _backend() != "cli":
        raise TranslateError("上網查只支援 cli 後端")
    user = title if not context else f"{title}\n\n（上下文，同一則貼文，不用翻：）\n{context[:1500]}"
    data = _run_claude(_SEARCH_HEAD + _COMMON_RULES + _glossary_text(), user, SEARCH_TIMEOUT,
                       ["--tools", "WebSearch", "--allowedTools", "WebSearch", "--json-schema", _SEARCH_SCHEMA])
    out = data.get("structured_output")
    if not isinstance(out, dict):
        try:
            out = json.loads(data.get("result") or "")
        except ValueError:
            raise TranslateError("查詢結果不是 JSON") from None
    cands = [c for c in out.get("candidates") or [] if isinstance(c, dict) and (c.get("text") or "").strip()]
    if not cands:
        raise TranslateError("查不到候選")
    return [{"text": c["text"].strip(), "why": (c.get("why") or "").strip(),
             "terms": [t for t in c.get("terms") or [] if t.get("term") and t.get("zh")]} for c in cands[:3]]


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
    data = _run_claude(system_prompt(post), title, CLI_TIMEOUT, ["--tools", ""])
    if data.get("stop_reason") == "refusal":
        raise TranslateError("Claude 拒絕翻譯")
    if data.get("stop_reason") == "max_tokens":
        raise TranslateError("譯文被截斷（超過 max_tokens）")
    text = (data.get("result") or "").strip()
    if not text:
        raise TranslateError("譯文是空的")
    return text


def _run_claude(system: str, user: str, timeout: int, tool_args: list[str]) -> dict:
    """claude -p：只給 tool_args 指定的工具、不讀設定檔和 MCP、不存 session，回傳 JSON 結果。"""
    cmd = [
        "claude", "-p", "--model", MODEL, "--effort", "low", "--system-prompt", system, *tool_args,
        "--strict-mcp-config", "--setting-sources", "",
        "--disable-slash-commands", "--no-session-persistence", "--output-format", "json",
    ]
    # 環境裡有 ANTHROPIC_API_KEY 的話 CLI 會改用它計費，要拿掉才會走訂閱
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        proc = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TranslateError(f"claude 指令跑不起來或逾時：{e}") from e
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise TranslateError(f"claude 輸出不是 JSON（exit {proc.returncode}）：{(proc.stderr or proc.stdout)[:300]}") from None
    if proc.returncode != 0 or data.get("is_error"):
        raise TranslateError(f"claude 回報錯誤：{str(data.get('result'))[:300]}")
    return data


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
            system=system_prompt(post),
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
