"""部署到遠端主機（例如 Synology NAS）：用 SSH exec channel 推檔（Synology 常停用 SFTP）→ docker compose up -d --build。

需要 paramiko：pip install paramiko

連線資訊放在 .env.deploy（不進 git）：
  DEPLOY_HOST=192.168.1.10
  DEPLOY_USER=me
  DEPLOY_PASS=...                     # SSH 密碼，也用在 sudo
  DEPLOY_DIR=/var/services/homes/me/fanbot
  DEPLOY_DOCKER=/usr/local/bin/docker # Synology 的 docker 路徑；一般 Linux 用 docker
  DEPLOY_SUDO=1                       # docker 要 sudo 才填 1

用法：
  python deploy.py         # 推檔 + build + 起 + 印 log
  python deploy.py --logs  # 只看 log
"""
import argparse
import shlex
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).parent
FILES = [
    "main.py", "translate.py", "notify.py", "notify_threads.py", "review.py", "ig_publish.py",
    "sources/__init__.py", "sources/youtube.py", "sources/weverse.py", "sources/instagram.py",
    "config.json", "glossary.json", "requirements.txt", "Dockerfile", "docker-compose.yml",
    ".dockerignore", ".env",
]


def read_env(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def run(ssh, cmd: str, pw: str, sudo: bool = False, check: bool = True) -> str:
    if sudo:
        cmd = f"echo {shlex.quote(pw)} | sudo -S -p '' sh -c {shlex.quote(cmd)}"
    _, stdout, stderr = ssh.exec_command(cmd, timeout=900)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    if check and code != 0:
        sys.exit(f"指令失敗 ({code})：{cmd[:80]}...\n{out}\n{err}")
    return out + err


def push(ssh, rel: str, remote_dir: str) -> None:
    remote = f"{remote_dir}/{rel}"
    stdin, stdout, _ = ssh.exec_command(f"mkdir -p {shlex.quote(str(Path(remote).parent))} && cat > {shlex.quote(remote)}")
    stdin.write((ROOT / rel).read_bytes())
    stdin.channel.shutdown_write()
    if stdout.channel.recv_exit_status() != 0:
        sys.exit(f"推檔失敗：{rel}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--logs", action="store_true")
    args = p.parse_args()

    cfg_path = ROOT / ".env.deploy"
    if not cfg_path.exists():
        sys.exit("缺 .env.deploy（見本檔開頭說明）")
    cfg = read_env(cfg_path)
    host, user, pw, remote_dir = cfg["DEPLOY_HOST"], cfg["DEPLOY_USER"], cfg["DEPLOY_PASS"], cfg["DEPLOY_DIR"]
    docker = cfg.get("DEPLOY_DOCKER", "docker")
    sudo = cfg.get("DEPLOY_SUDO") == "1"

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=user, password=pw, look_for_keys=False, allow_agent=False, timeout=20)

    compose = f"cd {shlex.quote(remote_dir)} && {docker} compose"
    if not args.logs:
        for rel in FILES:
            push(ssh, rel, remote_dir)
        run(ssh, f"chmod 600 {remote_dir}/.env && mkdir -p {remote_dir}/data", pw)
        print(f"推了 {len(FILES)} 個檔到 {remote_dir}")
        print(run(ssh, f"{compose} up -d --build --force-recreate 2>&1 | tail -15", pw, sudo=sudo))
    print(run(ssh, f"{compose} ps 2>&1; {compose} logs --tail 30 2>&1", pw, sudo=sudo, check=False))
    ssh.close()


if __name__ == "__main__":
    main()
