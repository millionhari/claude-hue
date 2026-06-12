#!/usr/bin/env python3
"""One-time onboarding for claude-hue.

Discovers the Hue bridge, registers a user, lets you pick lights or a group,
flashes through every state to confirm, and writes ~/.claude/hue_hooks/config.json.
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

CONFIG_DIR = Path.home() / ".claude" / "hue_hooks"
CONFIG_PATH = CONFIG_DIR / "config.json"

STATES = {
    "working": {"on": True, "xy": [0.21, 0.62], "bri": 254, "alert": "none", "transitiontime": 4},
    "idle":    {"on": True, "hue": 6500,  "sat": 254, "bri": 254, "alert": "none", "transitiontime": 4},
    "input":   {"on": True, "hue": 52000, "sat": 254, "bri": 254, "alert": "none", "transitiontime": 4},
    "off":     {"on": False, "transitiontime": 0},
}


def http_get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode())


def http_post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def http_put(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="PUT",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def discover_bridge():
    print("Discovering Hue bridge on local network...")
    found_ip = None
    try:
        results = http_get("https://discovery.meethue.com/")
        if results:
            found_ip = results[0].get("internalipaddress")
    except Exception as e:
        print(f"  Discovery failed: {e}")

    if found_ip:
        print(f"  Found bridge at {found_ip}")
        answer = input(f"Use {found_ip}? [Y/n, or type a different IP]: ").strip()
        if answer == "" or answer.lower() == "y":
            return found_ip
        return answer

    return input("Enter bridge IP manually: ").strip()


def register_user(bridge_ip):
    print("\nPress the link button on your Hue Bridge, then press Enter here...")
    input()
    url = f"http://{bridge_ip}/api"
    body = {"devicetype": "claude-hue#claudecode"}

    for attempt in range(1, 11):
        try:
            result = http_post(url, body)
        except Exception as e:
            print(f"  Attempt {attempt}/10 failed: {e}")
            time.sleep(1)
            continue

        if isinstance(result, list) and result:
            entry = result[0]
            if "success" in entry:
                username = entry["success"]["username"]
                print(f"  Registered. Username: {username[:8]}...{username[-4:]}")
                return username
            err = entry.get("error", {})
            if err.get("type") == 101:
                print(f"  Attempt {attempt}/10: link button not pressed, retrying...")
                time.sleep(1)
                continue
            print(f"  Unexpected response: {result}")
            sys.exit(1)

        print(f"  Unexpected response shape: {result}")
        time.sleep(1)

    print("\nFailed to register after 10 attempts. Press the link button BEFORE hitting Enter.")
    sys.exit(1)


def pick_target(bridge_ip, username):
    while True:
        choice = input("\nControl individual lights or a group? [lights/group]: ").strip().lower()
        if choice in ("lights", "l"):
            return pick_lights(bridge_ip, username)
        if choice in ("group", "g"):
            return pick_group(bridge_ip, username)
        print("  Please answer 'lights' or 'group'.")


def pick_lights(bridge_ip, username):
    lights = http_get(f"http://{bridge_ip}/api/{username}/lights")
    if not lights:
        print("No lights found on this bridge.")
        sys.exit(1)
    print("\nAvailable lights:")
    for lid, info in sorted(lights.items(), key=lambda x: int(x[0])):
        print(f"  {lid}: {info.get('name', '?')}")
    raw = input("Enter comma-separated light IDs (e.g. 1, 3, 5): ").strip()
    ids = [int(x.strip()) for x in raw.split(",") if x.strip()]
    if not ids:
        print("No IDs entered.")
        sys.exit(1)
    return {"light_ids": ids, "group_id": None}


def pick_group(bridge_ip, username):
    groups = http_get(f"http://{bridge_ip}/api/{username}/groups")
    if not groups:
        print("No groups found on this bridge.")
        sys.exit(1)
    print("\nAvailable groups:")
    for gid, info in sorted(groups.items(), key=lambda x: int(x[0])):
        gtype = info.get("type", "?")
        print(f"  {gid}: {info.get('name', '?')} ({gtype})")
    gid = int(input("Enter group ID: ").strip())
    return {"light_ids": [], "group_id": gid}


def apply_state(bridge_ip, username, target, state):
    payload = STATES[state]
    if target["group_id"] is not None:
        url = f"http://{bridge_ip}/api/{username}/groups/{target['group_id']}/action"
        http_put(url, payload)
    else:
        for lid in target["light_ids"]:
            url = f"http://{bridge_ip}/api/{username}/lights/{lid}/state"
            http_put(url, payload)


def test_states(bridge_ip, username, target):
    print("\nFlashing through states (working → idle → input → off → idle)...")
    for state in ("working", "idle", "input", "off", "idle"):
        print(f"  → {state}")
        try:
            apply_state(bridge_ip, username, target, state)
        except Exception as e:
            print(f"    failed: {e}")
        time.sleep(1)
    print("Done.")


def save_config(bridge_ip, username, target):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = {
        "bridge_ip": bridge_ip,
        "username": username,
        "light_ids": target["light_ids"],
        "group_id": target["group_id"],
    }
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    print(f"\nConfig saved to {CONFIG_PATH}")


def main():
    print("=== claude-hue setup ===\n")
    bridge_ip = discover_bridge()
    if not bridge_ip:
        print("No bridge IP — aborting.")
        sys.exit(1)
    username = register_user(bridge_ip)
    target = pick_target(bridge_ip, username)
    test_states(bridge_ip, username, target)
    save_config(bridge_ip, username, target)
    print("\nNext steps:")
    print("  1. mkdir -p ~/.claude/hue_hooks")
    print("  2. cp claude_hooks/hue_hook.sh claude_hooks/hue_hook.py ~/.claude/hue_hooks/")
    print("  3. chmod +x ~/.claude/hue_hooks/hue_hook.sh")
    print("  4. Merge claude_hooks/settings.json into ~/.claude/settings.json")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
