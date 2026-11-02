# kpop-fanbot

追蹤 K-pop 團體在 YouTube、Weverse、Instagram 的新內容，用 Claude 翻成繁體中文，推到 Telegram 頻道，並同步發到 Threads。

範例設定是 QWER（實際在跑的頻道：[t.me/qwer_whattf](https://t.me/qwer_whattf)）。換成別的團體只要改 `config.json`、`glossary.json` 和 `.env`。

## 功能

| 來源 | 抓什麼 | 需要的帳號 |
|---|---|---|
| YouTube | 官方頻道新影片／Shorts，翻標題 | 不用（RSS，失敗時改抓頻道頁面） |
| Weverse | 成員貼文（Artist 分頁）＋成員在自己貼文下的留言，翻全文 | 不用（網頁版 API 免登入） |
| Instagram | 官方＋成員帳號的新貼文，翻內文 | 自己的 IG 專業帳號＋FB 粉專＋Meta 開發者 app |

推播到：
- **Telegram 頻道**：純文字通知＋圖片相簿（最多 10 張）。成員留言用「回覆」掛在原貼文下面。
- **Threads**（選用）：同樣內容，上限 500 字、圖片輪播最多 20 張；成員留言發成原 Threads 貼文的回覆。

推播範例：
```
QWER Hina 在 Instagram 發佈了新貼文！📷

（譯文）

▶ https://www.instagram.com/p/...
#QWER #큐더블유이알
本文由 AI 自動翻譯上傳
```

### 行為規則
- **第一次跑某個來源**：既有內容只記錄進 `data/state.db`，不推播。之後只推新的。
- **翻譯失敗**（API 額度不足、錯誤、被拒、譯文截斷）：不推、下輪重試；同一則失敗 `TRANSLATE_MAX_ATTEMPTS` 次（預設 12，約 1 小時）就放棄。開始失敗、放棄、恢復時私訊 `TELEGRAM_ADMIN_CHAT_ID`。
- 只有 emoji／符號的內容不送翻譯，直接推原文（不加 AI 註記）。
- **推播失敗**（TG 掛掉）：不記 seen，下輪重試。Threads 失敗不影響 TG，最多重試 5 次。
- **Threads 一篇只有一個主題標籤**：只放第一個 hashtag，Threads 會把它轉成標籤、顯示時不帶 `#`。
- Weverse 成員留言：只檢查最近 7 天的成員貼文；成員在粉絲貼文下的留言抓不到（要登入）。
- 抓不到：IG 限時動態、IG 廣播頻道、成員在別人 IG 貼文下的留言（API 不提供）。

## 架構

```
main.py              輪詢迴圈：抓來源 → 比對 seen → 翻譯 → 推 TG → 排 Threads 佇列
translate.py         Claude API 翻譯（標題／貼文兩種 prompt＋詞彙表）
notify.py            推播文字格式（TG／Threads 共用）＋ Telegram 推播
notify_threads.py    Threads API 發文（container → publish；token 自動 refresh）
sources/youtube.py   YouTube RSS＋頻道頁面備援
sources/weverse.py   Weverse 網頁版 API（HMAC 簽章，key 失效會自動從 weverse.io 前端 JS 重抓）
sources/instagram.py IG Business Discovery
threads_setup.py     驗證／換長效 Threads token
meta_setup.py        換不會過期的 FB 粉專 token＋檢查 IG 帳號
deploy.py            SSH 推檔＋docker compose 部署
```

SQLite（`data/state.db`）表：`seen`（推過的內容）、`sources`（已初始化的來源）、`tg_messages`（貼文→TG message_id，留言回覆用）、`threads_queue`／`threads_posted`、`translate_fail`。

## 換成別的團體

1. 複製這個 repo（建議開 private repo，因為 `config.json` 會有你的設定）。
2. 改 `config.json`：
   - `name`：團名，會出現在推播開頭。
   - `hashtags`：推播最後的 hashtag，第一個也是 Threads 的主題標籤。
   - `youtube_channels[].id`：頻道 ID（`UC` 開頭）。打開頻道頁 → 檢視原始碼搜尋 `channelId`，或用 `https://www.youtube.com/@handle` 頁面的 `externalId`。小心同名的非官方頻道。
   - `weverse_communities[].id`：社群 ID，用網址路徑查：
     `python -c "from sources import weverse; print(weverse.community_id('qwer'))"`（`weverse.io/qwer` 的 `qwer`）。
     `show_reply_to`：成員回覆別人留言時，要不要附上被回覆的那則（粉絲暱稱不顯示）。
   - `instagram_accounts[]`：`username`、`author`（推播顯示的名字）、`label`（log 用）。對方必須是專業／創作者帳號。
   - 不要的來源整段刪掉即可。
3. 改 `glossary.json`：團名、成員名、粉絲名的固定譯名（原文 → 譯名）。Weverse 貼文作者名會用 Weverse 官方名，不用另外設。
4. 照下面「設定」填 `.env`。
5. 先 `python main.py --dry-run` 看抓到什麼，再部署。

## 設定

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
```

### 1. Telegram（必要）
1. 找 [@BotFather](https://t.me/BotFather) `/newbot` 拿 token → `TELEGRAM_BOT_TOKEN`。
2. 開一個頻道，把 bot 加成管理員（要有發文權限）。
3. `TELEGRAM_CHAT_ID`：公開頻道可以填 `@頻道名`；私人頻道是 `-100` 開頭的數字（把頻道訊息轉給 [@userinfobot](https://t.me/userinfobot) 之類的 bot 查，或用 bot API `getUpdates`）。
4. `TELEGRAM_ADMIN_CHAT_ID`（建議）：你自己的個人 chat id，收系統警示。先私訊 bot 一句話，再開 `https://api.telegram.org/bot<TOKEN>/getUpdates` 看 `chat.id`。

### 2. Claude API（必要）
[console.anthropic.com](https://console.anthropic.com) 建 API key → `ANTHROPIC_API_KEY`，帳戶要有額度。
模型 `claude-opus-5-5`（effort low）。估算：每則約 $0.002～0.02，一個團每月約 200 則 ≈ US$1～3。

### 3. Threads（選用）
1. [developers.facebook.com](https://developers.facebook.com) 建 app，使用案例選 **Threads API**，權限 `threads_basic`、`threads_content_publish`。
2. 應用程式角色 → 新增 **Threads 測試人員**，填你的 Threads 帳號。
3. 到 Threads app → 設定 → 帳號 → 網站權限 → **邀請**，接受邀請（沒接受會出現 `The user has not accepted the invite to test the app`）。
4. 在使用案例的設定頁產生 token → `THREADS_ACCESS_TOKEN`。短效 token 的話，同頁的 App Secret 填 `THREADS_APP_SECRET`，跑 `python threads_setup.py` 換成 60 天長效。
5. 之後 bot 會把 token 存在 `data/threads_token.json`，每 7 天自動 refresh。**換 token 時要刪掉這個檔**再部署。

### 4. Instagram（選用）
Business Discovery 只能用「自己的 IG 專業帳號」去查別人的專業帳號：
1. 你的 IG 切成 **專業帳號**（創作者或商家）。
2. 建一個 **Facebook 粉絲專頁**，粉專設定 → 已連結的帳號 → 連結這個 IG。
3. Meta 開發者 app（可以跟 Threads 同一個）→ 新增使用案例，選 Instagram 相關、**透過 Facebook 登入** 的那個。
4. 工具 → **Graph API 測試工具** → 選 app → 取得使用者存取權杖，勾：
   `pages_show_list`、`pages_read_engagement`、`instagram_basic`、`instagram_manage_insights`（少這個會回 `#10 Application does not have permission`）。
   登入時要勾選你的粉專和 IG。如果之前授權過、沒出現選擇畫面：Facebook 設定 → 商業整合 → 移除 app 後重來。
5. 填 `.env`：`META_USER_TOKEN`＝剛拿到的 token，`META_APP_SECRET`＝應用程式設定 → 基本資料 → 應用程式密鑰。
6. `python meta_setup.py`：換長效 token → 取 **不會過期的粉專 token**，寫回 `.env` 的 `META_PAGE_TOKEN`、`META_IG_USER_ID`，並檢查 `config.json` 的 IG 帳號都查得到。
   新版粉專不會出現在 `me/accounts`，腳本會改用 token 授權範圍裡的粉專 id。
7. 限流：app 每小時約 200 次。每個帳號每輪 1 次，5 個帳號 × 每 5 分鐘 = 60 次／小時；測試時呼叫太多會暫時回 `#4 Application request limit reached`，等一小時就好。
8. 改 FB 密碼或移除 app 授權會讓 token 失效，重做 4～6。

### `.env` 其他欄位
- `POLL_SECONDS`：輪詢間隔，預設 300。
- `BOT_NAME`：容器名稱和私訊警示的開頭，預設 `fanbot`。同一台主機跑多個團體時要各取不同名字。
- `TRANSLATE_MAX_ATTEMPTS`：預設 12。

## 執行

```bash
.venv/bin/python main.py --dry-run   # 只印會推什麼，不推、不寫 DB
.venv/bin/python main.py --once      # 跑一輪
.venv/bin/python main.py             # 常駐
```

## 部署

任何有 Docker 的主機：
```bash
docker compose up -d --build
docker compose logs -f
```
`data/` 是 volume，存 state.db 和 Threads token，搬家時要一起搬，不然會重新初始化。

遠端主機（例如 Synology NAS）可以用 `deploy.py`：在 `.env.deploy` 填 SSH 連線資訊（見檔案開頭），然後
```bash
pip install paramiko
python deploy.py          # 推檔 + build + 起
python deploy.py --logs   # 只看 log
```
改了 `config.json`／`glossary.json`／`.env` 都要重新部署。

## 注意
- `.env`、`.env.deploy`、`data/` 不要進 git。
- Weverse 用的是網頁版非公開 API，Weverse 改版可能會壞。
- 第一次跑以前先確認 YouTube 頻道 ID 是官方的，同名頻道很多。
