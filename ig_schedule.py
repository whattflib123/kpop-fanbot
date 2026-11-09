"""本機管理 IG 短影音排程：把影片推到遠端主機的 data/ig_schedule/，改 schedule.json。bot 時間到自動發（見 ig_publish.py）。

用法（要 paramiko；連線沿用部署設定：有 deploy_nas.py 就用它，沒有就讀 .env.deploy）：
  python ig_schedule.py add 影片.mp4 --at "2026-11-10 20:00" --caption-file ig.txt [--threads-file th.txt] [--cover-ms 1500]
  python ig_schedule.py add 影片.mp4 --at "2026-11-10 20:00" --caption "IG 文案" [--threads "Threads 文案"]
  python ig_schedule.py list          # 列排程和狀態（pending／posted／failed）
  python ig_schedule.py rm <id>       # 取消（連影片一起刪）

--at 是本機時區（台灣就是 +08:00）；文案多行用 --caption-file 比較好打。
"""
import argparse
import json
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import paramiko

ROOT = Path(__file__).parent
CAPTION_LIMIT = 2200


def _conn():
    """回 (ssh, 遠端專案目錄, docker 指令, 容器名, 密碼, docker 要不要 sudo)。"""
    sys.path.insert(0, str(ROOT))
    try:
        import deploy_nas as d  # qwer-update／h2h-fanbot
        env = {}
        for f in d.NAS_ENVS:
            if f.exists():
                env.update(d.read_env(f))
        host, user, pw, remote, docker, sudo = env["NAS_HOST"], env["NAS_USER"], env["NAS_PASS"], d.REMOTE_DIR, d.DOCKER, True
    except ImportError:
        import deploy as d  # 模板 kpop-fanbot
        cfg = d.read_env(ROOT / ".env.deploy")
        host, user, pw, remote = cfg["DEPLOY_HOST"], cfg["DEPLOY_USER"], cfg["DEPLOY_PASS"], cfg["DEPLOY_DIR"]
        docker, sudo = cfg.get("DEPLOY_DOCKER", "docker"), cfg.get("DEPLOY_SUDO") == "1"
    container = "bot"
    compose = ROOT / "docker-compose.yml"
    if compose.exists():
        for line in compose.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("container_name:"):
                container = line.split(":", 1)[1].strip()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=user, password=pw, look_for_keys=False, allow_agent=False, timeout=20)
    return ssh, remote, docker, container, pw, sudo


def _run(ssh, cmd: str, pw: str = "", sudo: bool = False) -> str:
    if sudo:
        cmd = f"echo {shlex.quote(pw)} | sudo -S -p '' sh -c {shlex.quote(cmd)}"
    _, out, err = ssh.exec_command(cmd, timeout=900)
    text = out.read().decode(errors="replace")
    e = err.read().decode(errors="replace")
    if out.channel.recv_exit_status() != 0:
        sys.exit(f"遠端指令失敗：{cmd[:80]}\n{text}{e}")
    return text


def _write(ssh, remote: str, data_iter) -> None:
    stdin, stdout, _ = ssh.exec_command(f"mkdir -p {shlex.quote(str(Path(remote).parent))} && cat > {shlex.quote(remote)}")
    for chunk in data_iter:
        stdin.write(chunk)
    stdin.channel.shutdown_write()
    if stdout.channel.recv_exit_status() != 0:
        sys.exit(f"寫檔失敗：{remote}")


def _load(ssh, sdir: str) -> list[dict]:
    raw = _run(ssh, f"cat {shlex.quote(sdir)}/schedule.json 2>/dev/null || echo '[]'")
    return json.loads(raw or "[]")


def _save(ssh, sdir: str, items: list[dict]) -> None:
    items.sort(key=lambda it: it["at"])
    body = json.dumps(items, ensure_ascii=False, indent=1).encode()
    _write(ssh, f"{sdir}/schedule.json.tmp", [body])
    _run(ssh, f"mv {shlex.quote(sdir)}/schedule.json.tmp {shlex.quote(sdir)}/schedule.json")


def _status(ssh, docker, container, pw, sudo) -> dict:
    py = ("import sqlite3,json;db=sqlite3.connect('/app/data/state.db');"
          "print(json.dumps({r[0]:r[1:] for r in db.execute('select id,status,permalink,error from ig_scheduled')}))")
    try:
        return json.loads(_run(ssh, f"{docker} exec {container} python -c {shlex.quote(py)}", pw, sudo).strip() or "{}")
    except (SystemExit, ValueError):
        return {}  # 容器還沒有這張表（沒部署新版）


def _probe(video: Path) -> None:
    """有 ffprobe 就檢查長度／尺寸，只警告不擋。"""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                              "stream=width,height,codec_name:format=duration", "-of", "json", str(video)],
                             capture_output=True, text=True, timeout=30).stdout
        info = json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return
    st = (info.get("streams") or [{}])[0]
    dur = float(info.get("format", {}).get("duration", 0) or 0)
    w, h = st.get("width", 0), st.get("height", 0)
    print(f"影片：{w}x{h} {st.get('codec_name')} {dur:.1f} 秒")
    if dur and not 3 <= dur <= 15 * 60:
        print("⚠️ Reels 長度要 3 秒～15 分鐘")
    if w and h and abs(w / h - 9 / 16) > 0.02:
        print("⚠️ 不是 9:16 直式，IG 可能會裁切或加黑邊")


def _text(arg: str | None, path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return (arg or "").strip()


def cmd_add(a) -> None:
    video = Path(a.video).expanduser()
    if not video.exists():
        sys.exit(f"找不到 {video}")
    at = datetime.strptime(a.at, "%Y-%m-%d %H:%M").astimezone()
    if at <= datetime.now().astimezone():
        sys.exit("時間已經過了")
    caption = _text(a.caption, a.caption_file)
    if len(caption) > CAPTION_LIMIT:
        sys.exit(f"IG 文案 {len(caption)} 字，超過 {CAPTION_LIMIT}")
    _probe(video)
    sid = f"{at:%Y%m%d-%H%M}-{video.stem}"[:80]
    item = {"id": sid, "video": sid + video.suffix.lower(), "at": at.isoformat(), "caption": caption}
    threads = _text(a.threads, a.threads_file)
    if threads:
        item["threads"] = threads
    if a.cover_ms is not None:
        item["cover_ms"] = a.cover_ms
    ssh, remote, *_ = _conn()
    sdir = f"{remote}/data/ig_schedule"
    items = _load(ssh, sdir)
    if any(it["id"] == sid for it in items):
        sys.exit(f"已經有 {sid}，先 rm 再加")
    size = video.stat().st_size
    print(f"上傳 {video.name}（{size / 1e6:.1f} MB）…")

    def chunks():
        with video.open("rb") as fh:
            while True:
                b = fh.read(1 << 20)
                if not b:
                    return
                yield b
    _write(ssh, f"{sdir}/{item['video']}", chunks())
    items.append(item)
    _save(ssh, sdir, items)
    print(f"已排程 {sid}：{at:%Y-%m-%d %H:%M}（{len(caption)} 字文案{'，含 Threads 文案' if threads else ''}）")


def cmd_list(a) -> None:
    ssh, remote, docker, container, pw, sudo = _conn()
    items = _load(ssh, f"{remote}/data/ig_schedule")
    st = _status(ssh, docker, container, pw, sudo)
    if not items:
        print("沒有排程")
    for it in items:
        status, link, err = st.get(it["id"], ["pending", None, None])
        when = datetime.fromisoformat(it["at"]).astimezone()
        extra = link or (f"錯誤：{err}" if err else "")
        print(f"{when:%m/%d %H:%M}  {status:8} {it['id']}  {extra}")
        print(f"    {it['caption'][:50].replace(chr(10), ' ')}")


def cmd_rm(a) -> None:
    ssh, remote, *_ = _conn()
    sdir = f"{remote}/data/ig_schedule"
    items = _load(ssh, sdir)
    keep = [it for it in items if it["id"] != a.id]
    if len(keep) == len(items):
        sys.exit(f"沒有 {a.id}")
    gone = next(it for it in items if it["id"] == a.id)
    _save(ssh, sdir, keep)
    _run(ssh, f"rm -f {shlex.quote(sdir)}/{shlex.quote(gone['video'])}")
    print(f"已取消 {a.id}")


def main() -> None:
    p = argparse.ArgumentParser(description="IG 短影音排程")
    sub = p.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add")
    add.add_argument("video")
    add.add_argument("--at", required=True, help='"YYYY-MM-DD HH:MM"（本機時區）')
    add.add_argument("--caption")
    add.add_argument("--caption-file")
    add.add_argument("--threads")
    add.add_argument("--threads-file")
    add.add_argument("--cover-ms", type=int)
    sub.add_parser("list")
    rm = sub.add_parser("rm")
    rm.add_argument("id")
    a = p.parse_args()
    {"add": cmd_add, "list": cmd_list, "rm": cmd_rm}[a.cmd](a)


if __name__ == "__main__":
    main()
