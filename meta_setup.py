"""Instagram（Meta Graph API）token 設定小工具（在 Mac 跑一次）。

1. .env 填 META_USER_TOKEN（Graph API 測試工具拿的使用者 token）
   和 META_APP_SECRET（app 的應用程式密鑰）
2. .venv/bin/python meta_setup.py
   → 換長效 user token → 取粉專 token（不會過期）＋ IG 商業帳號 id，寫回 .env
   → 用 Business Discovery 檢查 config 的 IG 帳號抓不抓得到。不會印出 token。
"""
import json
import os
import re
import sys

import requests

import main
from threads_setup import set_env

G = "https://graph.facebook.com/v21.0"


def _mask(s: str) -> str:
    return re.sub(r"access_token=[^&\s]+", "access_token=***", str(s))


def get(path: str, **params) -> dict:
    r = requests.get(f"{G}/{path}", params=params, timeout=30)
    data = r.json()
    if "error" in data:
        raise RuntimeError(_mask(data["error"].get("message")))
    return data


def main_() -> None:
    main.load_env()
    user = os.environ.get("META_USER_TOKEN")
    secret = os.environ.get("META_APP_SECRET")
    if not user:
        raise SystemExit(".env 沒有 META_USER_TOKEN")

    dbg = get("debug_token", input_token=user, access_token=user)["data"]
    print("app_id:", dbg.get("app_id"), "| 權限:", ",".join(dbg.get("scopes", [])),
          "| 過期:", dbg.get("expires_at") or "不會")

    if secret:
        try:
            user = get("oauth/access_token", grant_type="fb_exchange_token", client_id=dbg["app_id"],
                       client_secret=secret, fb_exchange_token=user)["access_token"]
            set_env("META_USER_TOKEN", user)
            print("已換成長效 user token，寫回 .env")
        except RuntimeError as e:
            print("換長效 token 失敗：", e)
    else:
        print("沒有 META_APP_SECRET：先用短效 token 測試（約 1 小時過期）")

    pages = get("me/accounts", fields="id,name,access_token,instagram_business_account{id,username}",
                access_token=user)["data"]
    if not pages:  # 新版粉專常不出現在 me/accounts，改用授權範圍裡的粉專 id 直接查
        ids = {i for s in dbg.get("granular_scopes", []) if s["scope"].startswith("pages_")
               for i in s.get("target_ids", [])}
        pages = [get(i, fields="id,name,access_token,instagram_business_account{id,username}", access_token=user)
                 for i in ids]
    page = next((p for p in pages if p.get("instagram_business_account")), None)
    print("粉專：", [(p["name"], (p.get("instagram_business_account") or {}).get("username")) for p in pages])
    if not page:
        raise SystemExit("找不到連結 IG 的粉專")
    ig_id = page["instagram_business_account"]["id"]
    token = page["access_token"]
    set_env("META_IG_USER_ID", ig_id)
    set_env("META_PAGE_TOKEN", token)
    exp = get("debug_token", input_token=token, access_token=token)["data"].get("expires_at")
    print(f"IG 帳號 {page['instagram_business_account']['username']}（{ig_id}），粉專 token 過期：{exp or '不會'}，已寫入 .env")

    accounts = sys.argv[1:] or [a["username"] for art in json.load(open("config.json"))["artists"]
                                for a in art.get("instagram_accounts", [])]
    for u in accounts:
        try:
            bd = get(ig_id, access_token=token, fields=f"business_discovery.username({u})"
                     "{username,name,followers_count,media_count,media.limit(1){timestamp,media_type,permalink}}")
            b = bd["business_discovery"]
            m = (b.get("media") or {}).get("data", [{}])[0]
            print(f"  OK {u}：{b.get('name')}｜{b.get('media_count')} 則｜最新 {m.get('timestamp')} {m.get('media_type')} {m.get('permalink')}")
        except RuntimeError as e:
            print(f"  NG {u}：{e}")


if __name__ == "__main__":
    main_()
