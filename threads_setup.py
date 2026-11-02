"""Threads token 設定小工具（在 Mac 跑一次）。

1. 把從 Meta 開發者後台拿到的 token 填進 .env 的 THREADS_ACCESS_TOKEN
   （有 App Secret 的話也填 THREADS_APP_SECRET，會自動換成 60 天長效 token）
2. .venv/bin/python threads_setup.py
   → 驗證 token、顯示帳號；短效 token 會換成長效並寫回 .env。不會印出 token。
"""
import re
from pathlib import Path

import requests

import main

ENV = Path(__file__).parent / ".env"


def set_env(key: str, value: str) -> None:
    text = ENV.read_text(encoding="utf-8")
    line = f"{key}={value}"
    text = re.sub(rf"^{key}=.*$", line, text, flags=re.M) if re.search(rf"^{key}=", text, re.M) else text + f"\n{line}\n"
    ENV.write_text(text, encoding="utf-8")


def main_() -> None:
    main.load_env()
    import os
    token = os.environ.get("THREADS_ACCESS_TOKEN")
    if not token:
        raise SystemExit(".env 沒有 THREADS_ACCESS_TOKEN")
    secret = os.environ.get("THREADS_APP_SECRET")
    if secret:
        r = requests.get("https://graph.threads.net/access_token",
                         params={"grant_type": "th_exchange_token", "client_secret": secret, "access_token": token}, timeout=20)
        if r.ok:
            data = r.json()
            token = data["access_token"]
            set_env("THREADS_ACCESS_TOKEN", token)
            print(f"已換成長效 token（{data.get('expires_in', 0) // 86400} 天），寫回 .env")
        else:
            print("換長效 token 失敗（可能本來就是長效）：", r.json().get("error", {}).get("message"))
    r = requests.get("https://graph.threads.net/v1.0/me", params={"fields": "id,username,name", "access_token": token}, timeout=20)
    print("帳號：", r.json() if r.ok else r.json().get("error", {}).get("message"))


if __name__ == "__main__":
    main_()
