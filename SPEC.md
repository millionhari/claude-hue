# SPEC: claude-hue

> Build a Claude Code hooks integration for Philips Hue lights, mirroring the
> architecture of https://github.com/bobek-balinek/claude-lamp but targeting
> the Philips Hue local bridge REST API instead of BLE.

---

## Goal

When Claude Code changes state (thinking, idle, waiting for input, done), the
user's Philips Hue lights should change colour to reflect that state — acting
as a physical ambient status indicator.

---

## Files to create

```
claude_hooks/
  setup.py          # One-time onboarding: discover bridge, register user, pick lights
  hue_hook.sh       # Shell script called by Claude Code on every hook event
  hue_hook.py       # Python script: reads state arg, sets light colour via Hue API
  hue_off.sh        # Manual kill switch: wipe state and force lamp off
  settings.json     # Claude Code hooks config — merge into ~/.claude/settings.json
README.md
```

Config is stored at `~/.claude/hue_hooks/config.json` (outside the repo, so it
works across all projects).

---

## States and colours

| State      | Colour         | Hue    | Sat | Bri | Alert | Trigger                                          |
|------------|----------------|--------|-----|-----|-------|--------------------------------------------------|
| `working`  | Green (= gauge "left" colour, CIE xy 0.21, 0.62) | — | — | 254 | none | UserPromptSubmit, PreToolUse |
| `idle`     | Warm amber     | 6500   | 254 | 254 | none  | Stop (as `done`), SessionStart, SessionEnd       |
| `input`    | Purple         | 52000  | 254 | 254 | none  | Notification (permission asks only)              |
| `off`      | Off            | —      | —   |  —  | none  | auto: 30 min all-sessions-idle. manual: `hue_off.sh` |

All lit states run at full brightness (254).

No pulse on `input` — a static purple is a clear enough "needs attention"
signal without nagging.

**Finish flash:** `Stop` invokes the hook as `done`. For state tracking,
`done` is plain `idle`; after applying the effective state, the hook sends
`{"alert": "select"}` `FLASH_COUNT` (2) times, `FLASH_GAP_SEC` (1.2s) apart —
two breathe cycles announcing the finished turn. Each `select` restores the
lamp's current colour afterwards, which is why the flash happens AFTER the
state is settled.

**Transparent states:** a state payload of `{"transparent": true}` means the
state doesn't own the lights. `apply()` then calls `restore_baseline()`
(no-op when nothing is captured) instead of painting. Before any real paint,
`capture_baseline()` snapshots every status light's v1 state (on/bri +
colormode-appropriate ct/xy/hue+sat; groups expanded via a bridge lookup) to
`/tmp/claude_hue_state/.baseline.json` — only if no snapshot exists. Restore
deletes the snapshot, so scene changes made while idle are re-captured on the
next takeover. Transparent states are excluded from the pulser and from
match-status gauge mode.

**Pulse:** tuning key `pulse_states` (list of state names) makes those states
pulse. `ensure_pulser()` runs after every state apply: it keeps exactly one
`_pulse <state>` child alive iff the effective state wants one (PID + state in
`/tmp/claude_hue_state/.pulse.json`), killing mismatched ones. The child
oscillates the status lights between `bri` and `PULSE_LOW_FRAC`·`bri` every
`PULSE_PERIOD_SEC`/2, self-terminates as soon as `loudest_state()` moves on,
and is killed by `apply("off")` so it never fights auto-sleep or `off-all`.
Dotfiles in the state dir (`.pulse.json`, `.gauge.json`) are skipped by
`loudest_state()` — the pulse file contains a `"state"` key that would
otherwise read as a phantom session.

**Context gauge:** an optional `gradient` block in `config.json` dedicates a
gradient lamp (Signe) to showing context-window usage as a bottom-up fill
bar — used fraction in red-orange (`GAUGE_USED_XY`), remaining in green
(`GAUGE_LEFT_XY`). On every hook event, `context_fraction()` tail-reads the
session transcript (last 512KB) for the most recent main-chain assistant
message and sums `input_tokens + cache_read_input_tokens +
cache_creation_input_tokens` over `context_limit`. The bar is painted via
the CLIP v2 API (`PUT /clip/v2/resource/light/{uuid}`, header
`hue-application-key`, HTTPS with verification off — v1 cannot address
gradient points) using `segmented_palette` mode for hard segment edges:
each of the `points` (5) segments is "used" if its centre `(i + 0.5)/n` is
≤ the used fraction. The filled-segment count is cached in
`/tmp/claude_hue_state/.gauge.json` and the PUT is skipped when unchanged —
repainting on every hook event makes the lamp blip on each tool call. The
cache is removed by `gauge_off()` (and by `off-all`'s state wipe) so the
first event after a sleep always repaints. The gauge lamp must NOT be in `light_ids`/`group_id`
(status writes would wipe the gradient); it is switched off alongside the
status lights by `gauge_off()` whenever the effective state is `off`.
Config schema: `gauges` — a list of lamps, each
`{light_id_v1, light_id_v2, points, reverse}`. The context window is
auto-detected per session by `detect_context_limit(model_id, used)`: `[1m]`
in the transcript's model id → 1M; else `[1m]` in the default model in
`~/.claude/settings.json` (when its base matches the session model) → 1M;
else `used > 200k` → 1M; else 200k. A top-level `context_limit` (or the
legacy value embedded in `gradient`) acts as a hard override. The legacy
single `gradient` block (with embedded `context_limit`) is honoured when
`gauges` is absent (`gauge_lamps()` / `gauge_context_limit()` normalise).
Status targets are `group_ids` (list) plus `light_ids` (list); legacy
`group_id` is folded in by `target_urls()`. The repaint cache stores
`{"filled": {<id_v2>: n, ...}}` and only lamps whose count changed are
re-PUT. **Gauge lamps outrank status targets:** the dashboard pre-resolves
the user's selection into a top-level `resolved_targets`
(`{group_ids, light_ids}`) where any group containing a gauge lamp is
expanded into its member lights minus the gauge lamps; `target_urls()`
prefers `resolved_targets` when present. Resolution happens at dashboard
save time, so group-membership changes in the Hue app need a re-save.
(Legacy `gauge_overlap: true` is still honoured by the bar painter for
hand-edited configs: it disables the repaint de-dup.)

**Match-status mode:** with tuning key `gauge_match_working` true, the gauge
yields while `effective` is `working` or `input`: the lamp is painted solid
with `STATES[effective]` via the v1 API (one PUT, deduped through the gauge
cache as `{"solid": "<state>"}` — so working→input transitions repaint), and
the status lights and the gauge glow the same colour. Idle/off fall through
to the normal bar paint, whose `filled` cache check misses against the solid
marker and repaints the bar immediately.

All state transitions use `"transitiontime": 4` (400ms) for smooth fades,
except `off` which is instant (`"transitiontime": 0`).

**Note on `PostToolUse`:** intentionally NOT wired. Firing `idle` after every
tool call would cause the light to strobe between blue-white and amber during
busy turns (10+ tool calls in a few seconds). The light stays `working`
through the entire turn and only goes `idle` when Claude finishes responding
(`Stop`).

**Note on `SessionEnd`:** goes to `idle` rather than `off` so the integration
doesn't hijack normal household lighting. This integration assumes you're
using a dedicated bulb as a status indicator. If you want the light to turn
off at session end, swap `idle` for `off` in `settings.json`.

---

## `setup.py` — one-time onboarding

Run once by the user: `python3 claude_hooks/setup.py`

Steps:
1. **Discover bridge** — GET `https://discovery.meethue.com/` and parse
   `internalipaddress` from the first result. Print it and ask the user to
   confirm or enter one manually.
2. **Register user** — Tell the user to press the button on their Hue Bridge,
   then wait for a keypress. POST to `http://{bridge_ip}/api` with body
   `{"devicetype": "claude-hue#claudecode"}`. Retry up to 10 times with a
   1-second delay if the bridge returns error type 101 ("link button not
   pressed"). On success, extract the `username` string.
3. **Pick target** — Ask first: "Control individual lights or a group?"
   - If **lights**: GET `http://{bridge_ip}/api/{username}/lights`, print a
     numbered list of names, and prompt for a comma-separated list of IDs
     (e.g. `1, 3, 5`).
   - If **group**: GET `http://{bridge_ip}/api/{username}/groups`, print a
     numbered list, and prompt for a single group ID.

   Targeting a group is preferred when controlling more than one bulb — the
   bridge applies the change atomically rather than sequentially.
4. **Test** — Flash the chosen lights through all four states (working →
   idle → input → off → idle) with a 1-second pause between each so the user
   can confirm everything works.
5. **Save config** — Write `~/.claude/hue_hooks/config.json`:

```json
{
  "bridge_ip": "192.168.1.X",
  "username": "abc123...",
  "light_ids": [1, 3],
  "group_id": null
}
```

Exactly one of `light_ids` or `group_id` will be populated based on the
user's choice in step 3. `hue_hook.py` checks `group_id` first; if set, it
hits the group endpoint, otherwise it loops over `light_ids`.

Use only Python stdlib (no third-party deps). Use `urllib.request` for HTTP.

This uses the Hue v1 (HTTP) API — universally supported, no TLS or
application-key dance. v2 (HTTPS + app keys) is a possible future enhancement
but unnecessary for a local-network status indicator.

---

## `hue_hook.sh`

Called by Claude Code on every hook event. Must:
- Accept `$1` as the state name (`working`, `idle`, `input`, `off`)
- Invoke `hue_hook.py` in the background (`&`) so it never blocks Claude Code
- Always `exit 0` — hook failures must never interrupt Claude Code

```bash
#!/bin/bash
STATE="${1:-idle}"
DIR="$(cd "$(dirname "$0")" && pwd)"
nohup python3 "$DIR/hue_hook.py" "$STATE" >> /tmp/claude_hue.log 2>&1 &
disown
exit 0
```

`nohup` + `disown` prevent the backgrounded Python process from being killed
by SIGHUP when the parent shell exits before the HTTP call completes.
Belt-and-braces — local bridge calls usually finish in ~50ms — but cheap
insurance.

---

## `hue_hook.py`

Called with a single argument: the state name.

Steps:
1. Load `~/.claude/hue_hooks/config.json`. If missing, log an error and exit 0.
2. Look up the state in the STATES table above.
3. Build the correct API URL:
   - If `group_id` is set: `http://{bridge_ip}/api/{username}/groups/{group_id}/action`
   - Otherwise, loop over `light_ids`: `http://{bridge_ip}/api/{username}/lights/{id}/state`
4. PUT the state payload as JSON.
5. Log success/failure to stdout (captured to `/tmp/claude_hue.log` by the shell wrapper).

Wrap everything in a top-level `try/except` — network errors, missing config,
bad JSON — all should log and exit 0. Never raise.

**Tuning overrides:** `apply_tuning(config)` runs right after config load.
An optional `tuning` block in `config.json` overrides, per key: `states`
(full v1 payloads per state name), `flash_count`, `flash_gap_sec`,
`idle_sleep_sec`, `gauge_used_xy`, `gauge_left_xy`. The module constants are
only defaults. This is the contract the dashboard (`dashboard.py` in
this repo) writes against; because the hook re-reads config on every event,
tuning changes are live immediately. `flash()` therefore resolves
`FLASH_COUNT` at call time, not in its default argument.

Use only Python stdlib. No third-party deps.

State payloads:

```python
STATES = {
    "working": {"on": True,  "xy": [0.21, 0.62],       "bri": 254, "alert": "none", "transitiontime": 4},
    "idle":    {"on": True,  "hue": 6500,  "sat": 254, "bri": 254, "alert": "none", "transitiontime": 4},
    "input":   {"on": True,  "hue": 52000, "sat": 254, "bri": 254, "alert": "none", "transitiontime": 4},
    "off":     {"on": False,                                                       "transitiontime": 0},
}
```

---

## `settings.json`

Ready to merge into `~/.claude/settings.json`. Paths point to
`~/.claude/hue_hooks/` so the hooks work across all projects.

```json
{
  "hooks": {
    "UserPromptSubmit":  [{"hooks": [{"type": "command", "command": "~/.claude/hue_hooks/hue_hook.sh working"}]}],
    "PreToolUse":        [{"hooks": [{"type": "command", "command": "~/.claude/hue_hooks/hue_hook.sh working"}]}],
    "Stop":              [{"hooks": [{"type": "command", "command": "~/.claude/hue_hooks/hue_hook.sh done"}]}],
    "SessionStart":      [{"hooks": [{"type": "command", "command": "~/.claude/hue_hooks/hue_hook.sh idle"}]}],
    "Notification":      [{"hooks": [{"type": "command", "command": "~/.claude/hue_hooks/hue_hook.sh input"}]}],
    "SessionEnd":        [{"hooks": [{"type": "command", "command": "~/.claude/hue_hooks/hue_hook.sh idle"}]}]
  }
}
```

---

## `README.md`

Include:
- What this is (one sentence)
- Prerequisites: Python 3.8+, Philips Hue Bridge on local network, no pip deps required
- Setup: `python3 claude_hooks/setup.py`, then copy files and merge settings
- Install command:
  ```bash
  mkdir -p ~/.claude/hue_hooks
  cp claude_hooks/hue_hook.sh claude_hooks/hue_hook.py ~/.claude/hue_hooks/
  chmod +x ~/.claude/hue_hooks/hue_hook.sh
  ```
- Troubleshooting: check `/tmp/claude_hue.log` (appended to on every hook
  fire — if you want to keep it small, run
  `: > /tmp/claude_hue.log` periodically or set up `logrotate`)
- Colour customisation: how to edit the STATES dict in `hue_hook.py`
- The state → colour table from this spec

---

## Notification disambiguation

Claude Code's `Notification` event fires for two unrelated reasons:

1. **Permission/decision requested** — message starts with *"Claude needs your permission to use ..."*
2. **User has been idle for ~60s** — message is *"Claude is waiting for your input"*

Both arrive on the same hook with the same JSON shape. Treating both as
`input` would latch the lamp purple every time the user finished a turn
and walked away from the terminal.

`hue_hook.py` reads the JSON payload from stdin and inspects `payload.message`.
If the message contains "waiting for your input" (case-insensitive), the
event is demoted from `input` to `idle`. Anything else keeps `input`.

This is a heuristic — if Anthropic changes the wording, the matcher silently
breaks back to the latching behaviour. The `msg=` field in the log line
exists so future drift can be diagnosed quickly.

---

## Auto sleep / wake

When every active session has been idle for `IDLE_SLEEP_SEC` (30 minutes
default), the lamp turns itself off. The next hook event from any session
wakes it back up — no manual intervention.

Implementation: each time `hue_hook.py`'s effective state lands on `idle`,
it spawns a tiny background watcher process (`python3 hue_hook.py
_sleep_watch`) and records its PID in `/tmp/claude_hue_state/.sleep.pid`.
The watcher sleeps `IDLE_SLEEP_SEC`, then re-evaluates state — if everything
is still idle, it sends `off`; if anything has gone working/input, it
no-ops. Each new idle event SIGTERMs the prior watcher and arms a fresh
one, so the timer resets correctly on activity.

This is the single concession to the "no daemon" rule — but the watcher
self-terminates after at most one timeout cycle, so it's closer to a
"long-running hook" than an always-on service.

Manual override: `hue_off.sh` wipes per-session state (so no straggler
can outvote it) and sends `off` immediately, for end-of-day shutdown.

---

## Multi-session coordination

Multiple Claude Code sessions can run concurrently and would otherwise fight
for the lamp (last-write-wins). To avoid that:

- Each hook event writes that session's current state to
  `/tmp/claude_hue_state/<session_id>.json` (atomic rename).
- After writing, scan all files in the directory; prune any with mtime older
  than `STATE_TTL_SEC` (30 minutes — handles crashed sessions that never
  fire `SessionEnd`).
- Pick the LOUDEST state across remaining sessions by priority:
  `input > working > idle > off`.
- PUT that state's payload to the bridge.

Result: the lamp reflects the most attention-worthy state across all
sessions. If any session needs input, lamp is purple. If any is working,
lamp is blue. Only when ALL sessions are idle does it go amber.

Session ID resolution:
1. JSON payload from stdin (Claude Code's canonical hook input — has `session_id`)
2. `$CLAUDE_SESSION_ID` env var
3. Literal `"default"` (used for manual CLI testing)

The shell wrapper must capture stdin synchronously before backgrounding the
Python child (otherwise `nohup` redirects stdin to `/dev/null` and the
session ID is lost).

---

## Constraints

- **No third-party dependencies** — stdlib only (`urllib.request`, `json`, `pathlib`, `sys`)
- **Never block Claude Code** — all Python runs in background, all scripts exit 0
- **Config outside the repo** — stored in `~/.claude/hue_hooks/` so it's global
- **Resilient** — every network call wrapped in try/except with logging
- **No persistent daemon** — unlike the BLE original, HTTP to a local bridge
  is fast enough that an always-on process is unnecessary; each hook fires a
  fresh Python call. The auto-sleep watcher (above) is the one exception:
  a self-terminating background process that exists for at most one
  `IDLE_SLEEP_SEC` cycle per idle event.
