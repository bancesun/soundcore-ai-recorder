#!/usr/bin/env python3
"""Run the Soundcore daily workflow from launchd or the terminal."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_DIR = ROOT / ".daily-run.lock"
LOG_DIR = ROOT / "logs"


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def ensure_nas_mounted() -> None:
    mount_point_raw = os.environ.get("NAS_MOUNT_POINT", "").strip()
    nas_url = os.environ.get("NAS_URL", "").strip()
    if not mount_point_raw or not nas_url:
        print(f"[{timestamp()}] NAS auto-mount skipped; NAS_URL or NAS_MOUNT_POINT is empty.")
        return

    mount_point = Path(mount_point_raw).expanduser()
    if mount_point.exists() and mount_point.is_mount():
        print(f"[{timestamp()}] NAS already mounted: {mount_point}")
        return

    wait_seconds = int(os.environ.get("NAS_MOUNT_WAIT_SECONDS", "120"))
    print(f"[{timestamp()}] Mounting NAS: {nas_url}")
    subprocess.run(["/usr/bin/open", nas_url], check=False)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if mount_point.exists() and mount_point.is_mount():
            print(f"[{timestamp()}] NAS mounted: {mount_point}")
            return
        time.sleep(2)
    raise RuntimeError(f"NAS did not mount: {mount_point}")


def main() -> int:
    load_env()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        LOCK_DIR.mkdir()
    except FileExistsError:
        print(f"[{timestamp()}] Another daily run is already active.")
        return 0

    caffeinate: subprocess.Popen[str] | None = None
    try:
        print(f"[{timestamp()}] Starting Soundcore daily workflow")
        print(f"[{timestamp()}] Workspace: {ROOT}")
        venv_python = ROOT / ".venv" / "bin" / "python"
        python = str(venv_python if venv_python.exists() else Path(sys.executable))

        if Path("/usr/bin/caffeinate").exists():
            caffeinate = subprocess.Popen(["/usr/bin/caffeinate", "-dimsu", "-w", str(os.getpid())])

        env = os.environ.copy()
        env.setdefault("HOME", str(Path.home()))
        env["PATH"] = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("REBUILD_EXISTING_DAILY", "false")

        try:
            ensure_nas_mounted()
            subprocess.run([python, "-u", str(ROOT / "scripts" / "soundcore_download.py")], cwd=str(ROOT), env=env, check=True, timeout=900)
            subprocess.run([python, "-u", str(ROOT / "scripts" / "process_recordings.py")], cwd=str(ROOT), env=env, check=True, timeout=7200)
        finally:
            if caffeinate is not None:
                caffeinate.terminate()

        print(f"[{timestamp()}] Finished Soundcore daily workflow")
        return 0
    finally:
        try:
            LOCK_DIR.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

