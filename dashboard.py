#!/usr/bin/env python3
"""claude-hue dashboard — local web console for tuning the lamp integration.

Serves a single-page UI and a small JSON API over 127.0.0.1. Edits are written
to ~/.claude/hue_hooks/config.json under a "tuning" block, which hue_hook.py
reads on every event — saves are live immediately, no restarts.

Usage: python3 dashboard.py [port]      (default 8420)

Stdlib only, like the hooks themselves.
"""

import json
import re
import ssl
import sys
import threading
import time
import urllib.request
import importlib.util
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

HOOKS_DIR = Path.home() / ".claude" / "hue_hooks"
CONFIG_PATH = HOOKS_DIR / "config.json"
STATE_DIR = Path("/tmp/claude_hue_state")
GAUGE_CACHE = STATE_DIR / ".gauge.json"
LOG_PATH = Path("/tmp/claude_hue.log")
STATIC_DIR = Path(__file__).resolve().parent / "static"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8420

# Import the installed hook module: its constants are the canonical defaults
# and its HTTP helpers already know how to talk to the bridge.
_spec = importlib.util.spec_from_file_location("hue_hook", HOOKS_DIR / "hue_hook.py")
hue = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hue)


def load_config():
    return json.loads(CONFIG_PATH.read_text())


def save_config(config):
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n")
    tmp.replace(CONFIG_PATH)


def bridge_get(config, path_v1):
    url = f"http://{config['bridge_ip']}/api/{config['username']}/{path_v1}"
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode())


def bridge_get_v2(config, path):
    req = urllib.request.Request(
        f"https://{config['bridge_ip']}{path}",
        headers={"hue-application-key": config["username"]},
    )
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
        return json.loads(r.read().decode())


def invalidate_gauge_cache():
    try:
        GAUGE_CACHE.unlink(missing_ok=True)
    except Exception:
        pass


def gather_status():
    sessions = []
    if STATE_DIR.exists():
        cutoff = time.time() - hue.STATE_TTL_SEC
        for f in STATE_DIR.glob("*.json"):
            if f.name.startswith("."):
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    continue
                data = json.loads(f.read_text())
                if data.get("state") in hue.PRIORITY:
                    sessions.append({
                        "id": f.stem,
                        "state": data["state"],
                        "age_sec": int(time.time() - data.get("ts", 0)),
                    })
            except Exception:
                continue
    effective = "idle"
    for s in hue.PRIORITY:
        if any(x["state"] == s for x in sessions):
            effective = s
            break

    filled = None
    try:
        filled = json.loads(GAUGE_CACHE.read_text()).get("filled")
    except Exception:
        pass

    log_tail, context_pct = [], None
    try:
        with open(LOG_PATH, "rb") as f:
            f.seek(max(0, f.seek(0, 2) - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
        log_tail = lines[-14:]
        for line in reversed(lines):
            m = re.search(r"gauge: (\d+)% of context used", line)
            if m:
                context_pct = int(m.group(1))
                break
    except Exception:
        pass

    return {
        "sessions": sorted(sessions, key=lambda x: x["age_sec"]),
        "effective": effective,
        "gauge_filled": filled,
        "context_pct": context_pct,
        "log": log_tail,
    }


def gather_bootstrap():
    config = load_config()
    out = {
        "config": config,
        "defaults": {
            "states": hue.STATES,
            "flash_count": hue.FLASH_COUNT,
            "flash_gap_sec": hue.FLASH_GAP_SEC,
            "idle_sleep_sec": hue.IDLE_SLEEP_SEC,
            "gauge_used_xy": hue.GAUGE_USED_XY,
            "gauge_left_xy": hue.GAUGE_LEFT_XY,
        },
        "lights": [],
        "groups": [],
        "gradient_lights": [],
        "bridge_ok": False,
    }
    try:
        lights = bridge_get(config, "lights")
        out["lights"] = [
            {"id": int(lid), "name": v.get("name", "?")}
            for lid, v in sorted(lights.items(), key=lambda x: int(x[0]))
        ]
        groups = bridge_get(config, "groups")
        out["groups"] = [
            {"id": int(gid), "name": v.get("name", "?"), "type": v.get("type", "?")}
            for gid, v in sorted(groups.items(), key=lambda x: int(x[0]))
            if v.get("type") in ("Room", "Zone", "LightGroup")
        ]
        out["bridge_ok"] = True
    except Exception:
        pass
    try:
        for l in bridge_get_v2(config, "/clip/v2/resource/light")["data"]:
            g = l.get("gradient")
            if g and g.get("points_capable"):
                out["gradient_lights"].append({
                    "id_v1": int(l["id_v1"].rsplit("/", 1)[-1]) if l.get("id_v1") else None,
                    "id_v2": l["id"],
                    "name": l["metadata"]["name"],
                    "points_capable": g["points_capable"],
                })
    except Exception:
        pass
    return out


def preview_state(body):
    """PUT a draft state payload straight to the status lights."""
    config = load_config()
    targets = dict(config)
    if "light_ids" in body:
        targets["light_ids"] = body["light_ids"]
        targets["group_id"] = body.get("group_id")
    payload = body["payload"]
    for url, _label in hue.target_urls(targets):
        hue.http_put(url, payload)


def preview_gauge(body):
    """Paint the gauge lamp with draft colours/fill via CLIP v2."""
    config = load_config()
    g = dict(config.get("gradient") or {})
    if body.get("light_id_v2"):
        g["light_id_v2"] = body["light_id_v2"]
    if not g.get("light_id_v2"):
        raise ValueError("no gradient light configured")
    n = int(body.get("points", g.get("points", 5)))
    filled = max(0, min(n, int(body.get("filled", 2))))
    used, left = body["used_xy"], body["left_xy"]
    points = [{"color": {"xy": used if i < filled else left}} for i in range(n)]
    if body.get("reverse"):
        points.reverse()
    hue.http_put_v2(
        config["bridge_ip"], config["username"],
        f"/clip/v2/resource/light/{g['light_id_v2']}",
        {"on": {"on": True}, "dimming": {"brightness": 100.0},
         "gradient": {"points": points, "mode": "segmented_palette"}},
    )
    # The hook's repaint de-dup must not mistake this preview for real state.
    invalidate_gauge_cache()


def preview_flash(body):
    config = load_config()
    count = max(1, min(10, int(body.get("count", 2))))
    gap = max(0.2, min(5.0, float(body.get("gap", 1.2))))

    def run():
        for _ in range(count):
            for url, _label in hue.target_urls(config):
                try:
                    hue.http_put(url, {"alert": "select"})
                except Exception:
                    pass
            time.sleep(gap)

    threading.Thread(target=run, daemon=True).start()


def all_off():
    config = load_config()
    hue.apply_tuning(config)
    if STATE_DIR.exists():
        for f in STATE_DIR.glob("*.json"):
            try:
                f.unlink()
            except Exception:
                pass
    hue.apply("off", config)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode()) if length else {}

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            data = (STATIC_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/api/bootstrap":
            self._json(gather_bootstrap())
        elif self.path == "/api/status":
            self._json(gather_status())
        else:
            self._json({"error": "not found"}, 404)

    def do_PUT(self):
        if self.path == "/api/config":
            try:
                config = self._body()
                if not config.get("bridge_ip") or not config.get("username"):
                    raise ValueError("config must keep bridge_ip and username")
                save_config(config)
                invalidate_gauge_cache()   # gauge colours may have changed
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 400)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            body = self._body()
            if self.path == "/api/preview/state":
                preview_state(body)
            elif self.path == "/api/preview/gauge":
                preview_gauge(body)
            elif self.path == "/api/preview/flash":
                preview_flash(body)
            elif self.path == "/api/off":
                all_off()
            else:
                return self._json({"error": "not found"}, 404)
            self._json({"ok": True})
        except Exception as e:
            self._json({"error": str(e)}, 400)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"claude-hue dashboard → http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
