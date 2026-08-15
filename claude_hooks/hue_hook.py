#!/usr/bin/env python3
"""Set Hue light state for a Claude Code hook event. Always exits 0.

Multi-session aware: each hook event writes the calling session's state to
/tmp/claude_hue_state/, then the lamp is set to the LOUDEST active state
across all known sessions. Priority (highest first): input > working > idle > off.

Stale entries (no update for 30 min) are pruned during each scan, so a crashed
session that never fires SessionEnd doesn't leave the lamp stuck.
"""

import json
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path.home() / ".claude" / "hue_hooks" / "config.json"
STATE_DIR = Path("/tmp/claude_hue_state")
SLEEP_PID_FILE = STATE_DIR / ".sleep.pid"
GAUGE_CACHE = STATE_DIR / ".gauge.json"
PULSE_FILE = STATE_DIR / ".pulse.json"
WATCHDOG_FILE = STATE_DIR / ".watchdog.pid"
BASELINE_FILE = STATE_DIR / ".baseline.json"
STATE_TTL_SEC = 30 * 60
IDLE_SLEEP_SEC = 30 * 60          # idle this long → lamp goes off
PRIORITY = ["input", "working", "idle", "off"]

STATES = {
    # working uses CIE xy, not hue/sat, to exactly match GAUGE_LEFT_XY below.
    "working": {"on": True, "xy": [0.21, 0.62], "bri": 254, "alert": "none", "transitiontime": 4},
    "idle":    {"on": True, "hue": 6500,  "sat": 254, "bri": 254, "alert": "none", "transitiontime": 4},
    "input":   {"on": True, "hue": 52000, "sat": 254, "bri": 254, "alert": "none", "transitiontime": 4},
    "off":     {"on": False, "transitiontime": 0},
}

FLASH_COUNT = 2
FLASH_GAP_SEC = 1.2               # one Hue "select" breathe cycle is ~1s

# Context gauge (gradient lamp as a bottom-up fill bar). CIE xy colours.
GAUGE_USED_XY = {"x": 0.62, "y": 0.35}   # consumed context: red-orange
GAUGE_LEFT_XY = {"x": 0.21, "y": 0.62}   # remaining context: green
GAUGE_MATCH_WORKING = False              # while working: gauge goes solid working-colour

# Brightness pulse, per state. Hue's own breathe alert stops after 15s, so a
# tiny background pulser process (like the sleep watcher) keeps it going.
PULSE_STATES = set()                     # e.g. {"working", "input"} via tuning

# No hook fires when the user interrupts (Escape) — Stop explicitly doesn't —
# so a watchdog demotes a "working" session to idle once its hook events AND
# its transcript have both been quiet this long.
WORK_STALE_SEC = 60
PULSE_PERIOD_SEC = 2.4                   # one full dim→bright→dim cycle
PULSE_LOW_FRAC = 0.25                    # dim phase as a fraction of state bri


def apply_tuning(config):
    """Everything above is just the default: a 'tuning' block in config.json
    (written by the dashboard) overrides per key, so colour/timing changes
    never require editing this file."""
    global FLASH_COUNT, FLASH_GAP_SEC, IDLE_SLEEP_SEC, GAUGE_USED_XY, GAUGE_LEFT_XY, GAUGE_MATCH_WORKING, PULSE_STATES
    t = config.get("tuning") or {}
    for name, payload in (t.get("states") or {}).items():
        if name in STATES and isinstance(payload, dict):
            STATES[name] = payload
    FLASH_COUNT = int(t.get("flash_count", FLASH_COUNT))
    FLASH_GAP_SEC = float(t.get("flash_gap_sec", FLASH_GAP_SEC))
    IDLE_SLEEP_SEC = int(t.get("idle_sleep_sec", IDLE_SLEEP_SEC))
    GAUGE_USED_XY = t.get("gauge_used_xy", GAUGE_USED_XY)
    GAUGE_LEFT_XY = t.get("gauge_left_xy", GAUGE_LEFT_XY)
    GAUGE_MATCH_WORKING = bool(t.get("gauge_match_working", GAUGE_MATCH_WORKING))
    PULSE_STATES = set(t.get("pulse_states") or [])
    global WORK_STALE_SEC
    WORK_STALE_SEC = int(t.get("working_stale_sec", WORK_STALE_SEC))


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def http_get(url, timeout=2):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_put(url, body, timeout=2):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="PUT",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def http_put_v2(bridge, app_key, path, body, timeout=2):
    """PUT to the CLIP v2 API (needed for gradients). The bridge serves HTTPS
    with a self-signed cert, so verification is off — it's a LAN device we
    already trust with the app key."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"https://{bridge}{path}", data=data, method="PUT",
        headers={"Content-Type": "application/json", "hue-application-key": app_key},
    )
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read().decode()


def read_payload():
    """Read Claude Code's hook JSON payload from stdin. Returns {} on any failure."""
    try:
        if not sys.stdin.isatty():
            data = sys.stdin.read()
            if data.strip():
                return json.loads(data)
    except Exception:
        pass
    return {}


def resolve_session_id(payload):
    sid = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or "default"
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in sid)[:64]


def is_idle_notification(payload):
    """Notification fires for two reasons: permission request OR idle-after-60s.
    The latter shouldn't latch the lamp at 'input' — distinguish by message text."""
    msg = (payload.get("message") or "").lower()
    return "waiting for your input" in msg or "waiting for input" in msg


def write_session_state(sid, state, transcript=None):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / f"{sid}.json"
    tmp = p.with_suffix(".tmp")
    entry = {"state": state, "ts": time.time()}
    if transcript:
        entry["transcript"] = transcript
    tmp.write_text(json.dumps(entry))
    tmp.replace(p)


def loudest_state():
    """Scan all session state files, prune stale ones, return the highest-priority active state."""
    if not STATE_DIR.exists():
        return "idle"
    cutoff = time.time() - STATE_TTL_SEC
    seen = set()
    for f in STATE_DIR.glob("*.json"):
        if f.name.startswith("."):       # .gauge.json / .pulse.json are not sessions
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                continue
            data = json.loads(f.read_text())
            s = data.get("state")
            if s in STATES:
                seen.add(s)
        except Exception:
            continue
    for s in PRIORITY:
        if s in seen:
            return s
    return "idle"


def schedule_sleep_watcher():
    """Reset the idle-sleep timer: kill any prior watcher, spawn a fresh one.
    The watcher sleeps IDLE_SLEEP_SEC then re-checks state — if everything is
    still idle, lamp goes off. Wake is automatic (next hook event re-applies
    the active state)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if SLEEP_PID_FILE.exists():
            old_pid = int(SLEEP_PID_FILE.read_text().strip())
            os.kill(old_pid, signal.SIGTERM)
    except Exception:
        pass

    log_fp = open("/tmp/claude_hue.log", "a")
    proc = subprocess.Popen(
        ["python3", str(Path(__file__).resolve()), "_sleep_watch"],
        stdin=subprocess.DEVNULL,
        stdout=log_fp,
        stderr=log_fp,
        start_new_session=True,
    )
    SLEEP_PID_FILE.write_text(str(proc.pid))
    log(f"sleep watcher armed (pid={proc.pid}, fires in {IDLE_SLEEP_SEC}s)")


def run_sleep_watcher(config):
    """Long-running watcher: wait, then turn off if state is still idle."""
    log(f"sleep watcher: pid={os.getpid()}, sleeping {IDLE_SLEEP_SEC}s")
    try:
        time.sleep(IDLE_SLEEP_SEC)
    except Exception:
        return
    eff = loudest_state()
    if eff == "idle":
        log(f"sleep watcher: still idle → off")
        apply("off", config)
    else:
        log(f"sleep watcher: state is {eff!r}, no-op")
    try:
        if SLEEP_PID_FILE.exists() and int(SLEEP_PID_FILE.read_text().strip()) == os.getpid():
            SLEEP_PID_FILE.unlink()
    except Exception:
        pass


def _status_targets(config):
    rt = config.get("resolved_targets")
    if isinstance(rt, dict):
        return rt.get("group_ids") or [], rt.get("light_ids") or []
    gids = config.get("group_ids")
    if gids is None:
        gids = [config["group_id"]] if config.get("group_id") is not None else []
    return gids, config.get("light_ids") or []


def target_urls(config):
    """Status targets: any mix of groups (zones/rooms) and individual lights.
    Gauge lamps outrank status: the dashboard pre-resolves the selection into
    'resolved_targets' with gauge-containing groups expanded to their member
    lights minus the gauge lamps. Legacy single group_id still works."""
    bridge = config["bridge_ip"]
    user = config["username"]
    gids, lids = _status_targets(config)
    urls = [(f"http://{bridge}/api/{user}/groups/{gid}/action", f"group {gid}")
            for gid in gids]
    urls += [(f"http://{bridge}/api/{user}/lights/{lid}/state", f"light {lid}")
             for lid in lids]
    return urls


def status_light_ids(config):
    """Every individual light covered by the status targets, with groups
    expanded via a bridge lookup (only used at baseline capture time)."""
    bridge = config["bridge_ip"]
    user = config["username"]
    gids, lids = _status_targets(config)
    out = set(int(x) for x in lids)
    for gid in gids:
        try:
            data = http_get(f"http://{bridge}/api/{user}/groups/{gid}")
            out.update(int(x) for x in data.get("lights", []))
        except Exception as e:
            log(f"group {gid} lookup failed: {e}")
    return sorted(out)


def capture_baseline(config):
    """Snapshot the status lights' current state before our first paint, so a
    'transparent' state can hand them back exactly as they were."""
    if BASELINE_FILE.exists():
        return
    bridge = config["bridge_ip"]
    user = config["username"]
    snap = {}
    for lid in status_light_ids(config):
        try:
            st = http_get(f"http://{bridge}/api/{user}/lights/{lid}")["state"]
            entry = {"on": bool(st.get("on")), "bri": st.get("bri", 254)}
            mode = st.get("colormode")
            if mode == "ct" and st.get("ct"):
                entry["ct"] = st["ct"]
            elif mode == "xy" and st.get("xy"):
                entry["xy"] = st["xy"]
            elif st.get("hue") is not None:
                entry["hue"], entry["sat"] = st["hue"], st.get("sat", 254)
            snap[str(lid)] = entry
        except Exception as e:
            log(f"baseline capture failed for light {lid}: {e}")
    if snap:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        BASELINE_FILE.write_text(json.dumps(snap))
        log(f"baseline captured ({len(snap)} light(s))")


def restore_baseline(config):
    """Put the status lights back to their captured pre-takeover state, then
    drop the snapshot so the next takeover captures fresh."""
    try:
        snap = json.loads(BASELINE_FILE.read_text())
    except Exception:
        return
    bridge = config["bridge_ip"]
    user = config["username"]
    for lid, entry in snap.items():
        payload = {"on": False, "transitiontime": 4} if not entry.get("on") \
            else dict(entry, transitiontime=4)
        try:
            http_put(f"http://{bridge}/api/{user}/lights/{lid}/state", payload)
        except Exception as e:
            log(f"baseline restore failed for light {lid}: {e}")
    try:
        BASELINE_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    log(f"baseline restored ({len(snap)} light(s))")


def gauge_lamps(config):
    """Gauge lamps: a 'gauges' list, with fallback to the legacy single
    'gradient' block."""
    g = config.get("gauges")
    if isinstance(g, list):
        return [l for l in g if l.get("light_id_v2")]
    old = config.get("gradient")
    if old and old.get("light_id_v2"):
        return [old]
    return []


def gauge_pulse_urls(config, state):
    """Gauge lamps to pulse alongside the status lights. Only when match-status
    is on and a busy state is showing the solid status colour — otherwise the
    gauge is a context fill bar and must not be touched."""
    if not (GAUGE_MATCH_WORKING and state in ("working", "input")):
        return []
    if (STATES.get(state) or {}).get("transparent"):
        return []
    bridge, user = config["bridge_ip"], config["username"]
    return [(f"http://{bridge}/api/{user}/lights/{l['light_id_v1']}/state",
             f"gauge {l['light_id_v1']}")
            for l in gauge_lamps(config) if l.get("light_id_v1") is not None]


def gauge_context_limit(config):
    """Explicit override only — None means auto-detect per session."""
    v = config.get("context_limit")
    if v and v != "auto":
        return int(v)
    legacy = (config.get("gradient") or {}).get("context_limit")
    return int(legacy) if legacy else None


def detect_context_limit(model_id, used):
    """Best-effort context window detection. The transcript's model id drops
    the [1m] suffix, so also consult the configured default model in
    ~/.claude/settings.json; usage beyond 200k proves a 1M window."""
    if model_id and "[1m]" in model_id:
        return 1_000_000
    try:
        settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        settings_model = settings.get("model") or ""
    except Exception:
        settings_model = ""
    if "[1m]" in settings_model and (not model_id or settings_model.replace("[1m]", "") == model_id):
        return 1_000_000
    if used > 200_000:
        return 1_000_000
    return 200_000


def apply(state, config):
    payload = STATES[state]
    if payload.get("transparent"):
        # This state doesn't own the lights: hand them back to whatever they
        # showed before we took over (no-op if nothing is captured).
        restore_baseline(config)
        return
    capture_baseline(config)
    for url, label in target_urls(config):
        try:
            http_put(url, payload)
            log(f"{state} → {label}")
        except Exception as e:
            log(f"PUT {url} failed: {e}")
    # The gauge lamp isn't a status target, but it should sleep with the rest;
    # a live pulser would fight the off state, so it dies here too.
    if state == "off":
        gauge_off(config)
        kill_pulser()


def flash(config, count=None):
    """Announce a finished turn: 'select' runs one breathe cycle and restores
    whatever colour the lamp was showing, so flash AFTER settling the state."""
    if count is None:
        count = FLASH_COUNT      # read at call time so tuning overrides apply
    for _ in range(count):
        for url, label in target_urls(config):
            try:
                http_put(url, {"alert": "select"})
            except Exception as e:
                log(f"flash PUT {url} failed: {e}")
        time.sleep(FLASH_GAP_SEC)
    log(f"flashed ×{count}")


def kill_pulser():
    try:
        info = json.loads(PULSE_FILE.read_text())
        os.kill(int(info["pid"]), signal.SIGTERM)
    except Exception:
        pass
    try:
        PULSE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def ensure_watchdog(config):
    """Keep one interrupt watchdog alive while anything is working."""
    try:
        os.kill(int(WATCHDOG_FILE.read_text().strip()), 0)
        return
    except Exception:
        pass
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_fp = open("/tmp/claude_hue.log", "a")
    proc = subprocess.Popen(
        ["python3", str(Path(__file__).resolve()), "_watchdog"],
        stdin=subprocess.DEVNULL, stdout=log_fp, stderr=log_fp,
        start_new_session=True,
    )
    WATCHDOG_FILE.write_text(str(proc.pid))
    log(f"interrupt watchdog armed (pid={proc.pid})")


def run_watchdog(config):
    """Poll working sessions. A session is alive if its state file was
    touched (hook events) or its transcript grew recently; an Escape-interrupt
    stops both, so after WORK_STALE_SEC of silence it is demoted to idle and
    the lamps are re-applied. Exits when nothing is working."""
    log(f"watchdog: pid={os.getpid()} polling (stale after {WORK_STALE_SEC}s)")
    deadline = time.time() + 4 * 3600
    while time.time() < deadline:
        time.sleep(10)
        any_working = False
        demoted, demoted_transcript = False, None
        for f in STATE_DIR.glob("*.json"):
            if f.name.startswith("."):
                continue
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue
            if data.get("state") != "working":
                continue
            alive = f.stat().st_mtime
            tr = data.get("transcript")
            if tr:
                try:
                    alive = max(alive, os.path.getmtime(tr))
                except Exception:
                    pass
            if time.time() - alive > WORK_STALE_SEC:
                data["state"] = "idle"
                data["ts"] = time.time()
                f.write_text(json.dumps(data))
                demoted, demoted_transcript = True, tr or demoted_transcript
                log(f"watchdog: session {f.stem} quiet {int(time.time() - alive)}s → idle")
            else:
                any_working = True
        if demoted:
            effective = loudest_state()
            apply(effective, config)
            ensure_pulser(effective, config)
            update_context_gauge({"transcript_path": demoted_transcript}, config, effective)
            if effective == "idle":
                schedule_sleep_watcher()
        if not any_working:
            break
    try:
        if int(WATCHDOG_FILE.read_text().strip()) == os.getpid():
            WATCHDOG_FILE.unlink()
    except Exception:
        pass
    log(f"watchdog: pid={os.getpid()} exiting")


def ensure_pulser(effective, config):
    """Keep exactly one pulser alive iff the effective state wants one.
    Transparent states own nothing, so they never pulse."""
    want = effective in PULSE_STATES and not (STATES.get(effective) or {}).get("transparent")
    try:
        info = json.loads(PULSE_FILE.read_text())
        os.kill(int(info["pid"]), 0)          # raises if the pulser died
        if want and info.get("state") == effective:
            return                            # the right pulser is already running
        kill_pulser()
    except Exception:
        try:
            PULSE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    if not want:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_fp = open("/tmp/claude_hue.log", "a")
    proc = subprocess.Popen(
        ["python3", str(Path(__file__).resolve()), "_pulse", effective],
        stdin=subprocess.DEVNULL, stdout=log_fp, stderr=log_fp,
        start_new_session=True,
    )
    PULSE_FILE.write_text(json.dumps({"pid": proc.pid, "state": effective}))
    log(f"pulser armed for {effective!r} (pid={proc.pid})")


def run_pulser(state, config):
    """Long-running: oscillate the status lights' brightness while `state`
    stays effective. Self-terminates the moment the state moves on."""
    base = STATES.get(state) or {}
    hi = int(base.get("bri", 254))
    lo = max(5, int(hi * PULSE_LOW_FRAC))
    half = PULSE_PERIOD_SEC / 2
    tt = max(1, int(half * 10) - 2)          # fade almost the whole half-period
    log(f"pulser: pid={os.getpid()} state={state} bri {hi}↔{lo}")
    level = lo
    deadline = time.time() + 4 * 3600        # runaway backstop
    while time.time() < deadline:
        if loudest_state() != state:
            break
        urls = target_urls(config) + gauge_pulse_urls(config, state)
        for url, _label in urls:
            try:
                http_put(url, {"bri": level, "transitiontime": tt})
            except Exception:
                pass
        level = hi if level == lo else lo
        time.sleep(half)
    if loudest_state() == state:
        apply(state, config)                  # backstop exit: restore steady bri
    try:
        if json.loads(PULSE_FILE.read_text()).get("pid") == os.getpid():
            PULSE_FILE.unlink()
    except Exception:
        pass
    log(f"pulser: pid={os.getpid()} exiting")


def context_fraction(transcript_path, limit=None):
    """Fraction of the context window consumed, from the usage block of the
    most recent main-chain assistant message in the transcript JSONL. Only the
    tail of the file is read — transcripts grow to many MB. limit None →
    auto-detect from the message's model."""
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as f:
            f.seek(max(0, size - 512 * 1024))
            tail = f.read().decode("utf-8", "replace")
    except Exception:
        return None
    for line in reversed(tail.splitlines()):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        msg = entry.get("message") or {}
        usage = msg.get("usage") or {}
        used = (usage.get("input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0))
        if used:
            lim = limit or detect_context_limit(msg.get("model"), used)
            return min(used / lim, 1.0)
    return None


def update_context_gauge(payload, config, effective):
    """Paint every gauge lamp as a bottom-up fill bar: used context from the
    bottom in GAUGE_USED_XY, remaining headroom above in GAUGE_LEFT_XY.
    With gauge_match_working on, the bars yield to a solid status colour
    whenever any session is busy."""
    lamps = gauge_lamps(config)
    if not lamps:
        return

    if (GAUGE_MATCH_WORKING and effective in ("working", "input")
            and not (STATES.get(effective) or {}).get("transparent")):
        try:
            if json.loads(GAUGE_CACHE.read_text()).get("solid") == effective:
                return
        except Exception:
            pass
        for lamp in lamps:
            if lamp.get("light_id_v1") is None:
                continue
            url = f"http://{config['bridge_ip']}/api/{config['username']}/lights/{lamp['light_id_v1']}/state"
            try:
                http_put(url, STATES[effective])
            except Exception as e:
                log(f"gauge solid PUT failed: {e}")
        log(f"gauge: solid {effective} (match-status on, {len(lamps)} lamp(s))")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        GAUGE_CACHE.write_text(json.dumps({"solid": effective}))
        return

    transcript = payload.get("transcript_path")
    if not transcript:
        return
    frac = context_fraction(transcript, gauge_context_limit(config))
    if frac is None:
        return

    # Hooks fire on every tool call; re-PUTting an unchanged gradient makes
    # a lamp blip. Repaint only the lamps whose segment count changed. The
    # cache is cleared on gauge_off/off-all so wake-up always repaints.
    desired = {}
    for lamp in lamps:
        n = int(lamp.get("points", 5))
        desired[str(lamp["light_id_v2"])] = sum(1 for i in range(n) if (i + 0.5) / n <= frac)
    try:
        cached = json.loads(GAUGE_CACHE.read_text()).get("filled")
    except Exception:
        cached = None
    # gauge_overlap: a status target contains a gauge lamp, so every status
    # write wipes the gradient — always repaint instead of de-duping.
    if cached == desired and not config.get("gauge_overlap"):
        return
    prev = cached if isinstance(cached, dict) else {}
    if config.get("gauge_overlap"):
        prev = {}

    painted = 0
    for lamp in lamps:
        key = str(lamp["light_id_v2"])
        if prev.get(key) == desired[key]:
            continue
        n = int(lamp.get("points", 5))
        filled = desired[key]
        points = [
            {"color": {"xy": GAUGE_USED_XY if i < filled else GAUGE_LEFT_XY}}
            for i in range(n)
        ]
        if lamp.get("reverse"):
            points.reverse()
        body = {
            "on": {"on": True},
            "dimming": {"brightness": 100.0},
            "gradient": {"points": points, "mode": "segmented_palette"},
        }
        try:
            http_put_v2(config["bridge_ip"], config["username"],
                        f"/clip/v2/resource/light/{lamp['light_id_v2']}", body)
            painted += 1
        except Exception as e:
            log(f"gauge PUT failed: {e}")
    if painted:
        log(f"gauge: {frac:.0%} of context used ({painted} lamp(s) repainted)")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    GAUGE_CACHE.write_text(json.dumps({"filled": desired}))


def gauge_off(config):
    for lamp in gauge_lamps(config):
        if lamp.get("light_id_v1") is None:
            continue
        url = f"http://{config['bridge_ip']}/api/{config['username']}/lights/{lamp['light_id_v1']}/state"
        try:
            http_put(url, {"on": False, "transitiontime": 0})
            log(f"gauge off → light {lamp['light_id_v1']}")
        except Exception as e:
            log(f"gauge off failed: {e}")
    try:
        GAUGE_CACHE.unlink(missing_ok=True)
    except Exception:
        pass


def main():
    arg_state = sys.argv[1] if len(sys.argv) > 1 else "idle"

    try:
        config = json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        log(f"Config not found at {CONFIG_PATH}; run setup.py first")
        return
    except Exception as e:
        log(f"Failed to load config: {e}")
        return

    if not config.get("bridge_ip") or not config.get("username"):
        log("Config missing bridge_ip or username")
        return

    apply_tuning(config)

    # "off-all": user is done for the day. Wipe per-session state so no
    # straggler can outvote us, then turn the lamp off.
    if arg_state == "off-all":
        if STATE_DIR.exists():
            for f in STATE_DIR.glob("*.json"):
                try:
                    f.unlink()
                except Exception:
                    pass
        log("off-all: cleared session state, sending off")
        apply("off", config)
        return

    # "_sleep_watch": internal mode — backgrounded child that times out idle.
    if arg_state == "_sleep_watch":
        run_sleep_watcher(config)
        return

    # "_watchdog": internal mode — demotes interrupted sessions to idle.
    if arg_state == "_watchdog":
        run_watchdog(config)
        return

    # "_pulse <state>": internal mode — backgrounded brightness oscillator.
    if arg_state == "_pulse":
        run_pulser(sys.argv[2] if len(sys.argv) > 2 else "idle", config)
        return

    # "done": Stop event. Tracked as plain idle, but flashes after settling
    # so a finished turn is noticeable from across the room.
    flash_after = arg_state == "done"
    if flash_after:
        arg_state = "idle"

    if arg_state not in STATES:
        log(f"Unknown state: {arg_state!r}")
        return

    payload = read_payload()
    sid = resolve_session_id(payload)

    # The idle-warning Notification ("Claude is waiting for your input") fires
    # after ~60s of user inactivity and is NOT a permission ask. Treating it
    # as 'input' would latch the lamp purple every time you walked away.
    if arg_state == "input" and is_idle_notification(payload):
        arg_state = "idle"

    write_session_state(sid, arg_state, payload.get("transcript_path"))
    effective = loudest_state()
    msg = payload.get("message") if isinstance(payload.get("message"), str) else None
    log(f"session={sid} wrote={arg_state} effective={effective} msg={msg!r}")
    apply(effective, config)
    ensure_pulser(effective, config)
    if effective == "working":
        ensure_watchdog(config)
    update_context_gauge(payload, config, effective)

    if flash_after:
        flash(config)

    # Reset the sleep timer whenever effective is idle. Working/input cancels
    # nothing — the prior watcher will harmlessly no-op when it wakes and
    # sees a non-idle state.
    if effective == "idle":
        schedule_sleep_watcher()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            log(f"Top-level error: {e}")
        except Exception:
            pass
    sys.exit(0)
