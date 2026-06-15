#!/usr/bin/env python3
"""Install or update the macOS LaunchAgent for the daily workflow."""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.soundcore-ai-recorder.daily"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hour", type=int, default=22)
    parser.add_argument("--minute", type=int, default=30)
    parser.add_argument("--label", default=LABEL)
    args = parser.parse_args()

    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / f"{args.label}.plist"

    data = {
        "Label": args.label,
        "ProgramArguments": ["/usr/bin/python3", str(ROOT / "scripts" / "run_daily.py")],
        "RunAtLoad": False,
        "StartCalendarInterval": {"Hour": args.hour, "Minute": args.minute},
        "StandardOutPath": str(ROOT / "logs" / "daily.out.log"),
        "StandardErrorPath": str(ROOT / "logs" / "daily.err.log"),
    }
    with plist_path.open("wb") as handle:
        plistlib.dump(data, handle, sort_keys=False)

    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{args.label}"], check=False, capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)], check=True)
    subprocess.run(["launchctl", "enable", f"gui/{uid}/{args.label}"], check=False)

    print(f"Installed {plist_path}")
    print(f"Daily run time: {args.hour:02d}:{args.minute:02d}")
    print(f"Check status: launchctl print gui/$(id -u)/{args.label}")


if __name__ == "__main__":
    main()

