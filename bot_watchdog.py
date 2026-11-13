"""看門狗：主迴圈太久沒動（卡在網路之類）就讓 bot 自己結束，Docker（restart: unless-stopped）會自動重開。

2026-11-13 NAS 跟 Telegram 連線一直被重設，主迴圈卡在讀 TG 更新，按鈕都沒反應，要手動重開容器。
Telegram 那一呼叫本身有硬上限（notify.get_updates）；這裡是最後一道保險，任何地方卡住都會救回來。

主迴圈每輪、review.wait 每次長輪詢前呼叫 kick()。超過 WATCHDOG_SEC（預設 45 分鐘）沒 kick 就結束。
45 分鐘是因為 IG 處理影片最多等 15 分鐘、一輪可能還要傳 FB／YT，正常發文不能被誤殺。
WATCHDOG_SEC=0 關掉。重開不會重推已推過的通知（seen 表）。
"""
import logging
import os
import threading
import time

_last = [time.time()]


def kick() -> None:
    _last[0] = time.time()


def start() -> None:
    limit = int(os.environ.get("WATCHDOG_SEC", 45 * 60))
    if limit <= 0:
        return

    def loop() -> None:
        while True:
            time.sleep(60)
            idle = time.time() - _last[0]
            if idle > limit:
                logging.error("看門狗：主迴圈 %.0f 分鐘沒動，結束讓 Docker 重開", idle / 60)
                logging.shutdown()
                os._exit(1)

    kick()
    threading.Thread(target=loop, name="watchdog", daemon=True).start()
