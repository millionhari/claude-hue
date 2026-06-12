#!/usr/bin/env python3
"""Claude Hue.app entry point.

Idempotent: installs/refreshes the hook scripts and dashboard into
~/.claude/hue_hooks, wires the hook commands into Claude Code's
~/.claude/settings.json (preserving anything already there), starts the
dashboard server if it isn't running, and opens it. Relaunching the app just
brings the dashboard back up.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

RES = Path(__file__).resolve().parent
PAYLOAD = RES / "payload"
HOOKS_DIR = Path.home() / ".claude" / "hue_hooks"
DASH_DIR = HOOKS_DIR / "dashboard"
SETTINGS = Path.home() / ".claude" / "settings.json"
PORT = 8420

HOOK_EVENTS = {
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "Stop": "done",
    "SessionStart": "idle",
    "Notification": "input",
    "SessionEnd": "idle",
}


def install_files():
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("hue_hook.py", "hue_hook.sh", "hue_off.sh"):
        shutil.copy2(PAYLOAD / "claude_hooks" / name, HOOKS_DIR / name)
        os.chmod(HOOKS_DIR / name, 0o755)
    (DASH_DIR / "static").mkdir(parents=True, exist_ok=True)
    shutil.copy2(PAYLOAD / "dashboard.py", DASH_DIR / "dashboard.py")
    shutil.copy2(PAYLOAD / "static" / "index.html", DASH_DIR / "static" / "index.html")


def merge_settings():
    """Add the hue hook commands to Claude Code's settings without disturbing
    any hooks the user already has. No-op when already wired."""
    settings = {}
    if SETTINGS.exists():
        try:
            settings = json.loads(SETTINGS.read_text())
        except Exception:
            settings = {}
        backup = SETTINGS.parent / "settings.json.bak-hue"
        if not backup.exists():
            shutil.copy2(SETTINGS, backup)
    hooks = settings.setdefault("hooks", {})
    changed = False
    for event, state in HOOK_EVENTS.items():
        cmd = f"~/.claude/hue_hooks/hue_hook.sh {state}"
        groups = hooks.setdefault(event, [])
        existing = [h.get("command", "") for g in groups for h in g.get("hooks", [])]
        if any("hue_hooks/hue_hook.sh" in c for c in existing):
            continue
        if groups:
            groups[0].setdefault("hooks", []).append({"type": "command", "command": cmd})
        else:
            groups.append({"hooks": [{"type": "command", "command": cmd}]})
        changed = True
    if changed or not SETTINGS.exists():
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    return changed


def server_running():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=1)
        return True
    except Exception:
        return False


def start_server():
    if server_running():
        return
    log = open("/tmp/claude_hue_dashboard.log", "a")
    subprocess.Popen(
        [sys.executable or "python3", str(DASH_DIR / "dashboard.py")],
        stdout=log, stderr=log,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    for _ in range(40):
        if server_running():
            return
        time.sleep(0.2)


def notify(msg):
    try:
        safe = msg.replace('"', "'")
        subprocess.run(
            ["osascript", "-e", f'display notification "{safe}" with title "Claude Hue"'],
            check=False, capture_output=True,
        )
    except Exception:
        pass


def main():
    install_files()
    changed = merge_settings()
    start_server()
    if "--no-open" not in sys.argv:
        webbrowser.open(f"http://127.0.0.1:{PORT}")
    notify("Hooks installed — restart open Claude Code sessions to pick them up"
           if changed else "Dashboard is running")


if __name__ == "__main__":
    main()
