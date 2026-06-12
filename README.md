# claude-hue

Philips Hue ambient status indicator for Claude Code, with a local web
dashboard to tune everything. Your lights change colour to reflect what
Claude is doing — one colour while working, another when idle, another when
waiting on you — and a gradient lamp can double as a context-window gauge.
After 30 minutes of nobody touching anything, the lamps turn themselves off;
the next hook event wakes them back up.

The hook engine derives from
[Rondieli/claude-lamp-philips-hue](https://github.com/Rondieli/claude-lamp-philips-hue),
itself inspired by
[bobek-balinek/claude-lamp](https://github.com/bobek-balinek/claude-lamp);
it talks to a Hue Bridge over its local REST API instead of BLE.

## Prerequisites

- Python 3.8+
- A Philips Hue Bridge on your local network
- No `pip` dependencies — stdlib only

## Setup

Run the onboarding script. It discovers your bridge, registers a user, lets you
pick lights or a group, and flashes the chosen lights through every state so
you can confirm everything works.

```bash
python3 claude_hooks/setup.py
```

Then install the hook scripts globally so they fire across every Claude Code
project:

```bash
mkdir -p ~/.claude/hue_hooks
cp claude_hooks/hue_hook.sh claude_hooks/hue_hook.py claude_hooks/hue_off.sh ~/.claude/hue_hooks/
chmod +x ~/.claude/hue_hooks/hue_hook.sh ~/.claude/hue_hooks/hue_off.sh
```

Finally, merge `claude_hooks/settings.json` into `~/.claude/settings.json`. If
you don't already have a `hooks` block, you can copy the file wholesale.

## Dashboard

Everything below is configurable from a local web console — no config-file
editing required:

```bash
python3 dashboard.py        # → http://127.0.0.1:8420
```

Stdlib only, binds to localhost. It shows your lamps live (current state,
context gauge fill, active sessions, log), and edits status colors with a
custom wheel picker, brightness, the finish flash, pulse and keep-scene per
state, gauge lamps and colors, targets (any mix of zones, rooms, lights, and
multiple gauge lamps), sleep timing, and theme palettes — ten built-ins plus
your own saved themes, with instant on-lamp preview. Saves are written to
`~/.claude/hue_hooks/config.json` and are live on the next hook event.

## States

| State     | Colour       | Triggers                                          |
|-----------|--------------|---------------------------------------------------|
| `working` | Green (same as the gauge's free-context green) | UserPromptSubmit, PreToolUse |
| `idle`    | Warm amber   | Stop (as `done` — see below), SessionStart, SessionEnd |
| `input`   | Purple       | Notification (permission asks only — see below)   |
| `off`     | Off          | auto: 30 min of all-sessions-idle. manual: `hue_off.sh` |

All lit states run at full brightness (`bri` 254).

`Stop` fires the hook as `done`: the lamp settles to its effective state, then
flashes two breathe cycles (`alert: select`) to announce the finished turn.
For state-tracking purposes `done` is plain `idle`.

`PostToolUse` is intentionally not wired — firing on every tool call would
strobe the light during busy turns. The light stays `working` through the
whole turn and only flips to `idle` when Claude finishes responding.

`SessionEnd` goes to `idle` rather than `off` so the integration doesn't
hijack normal household lighting. If you're using a dedicated bulb and want
it to switch off when sessions end, change `idle` → `off` for `SessionEnd` in
`settings.json`.

## Auto sleep / wake

Whenever every session has been idle for `IDLE_SLEEP_SEC` (30 minutes by
default), the lamp turns itself off. The next hook event from any session
wakes it back up — no manual intervention needed.

Mechanically: each time the effective state lands on `idle`, `hue_hook.py`
spawns a small background watcher process (PID file at
`/tmp/claude_hue_state/.sleep.pid`). The watcher sleeps for `IDLE_SLEEP_SEC`
and then re-checks state — if everything's still idle, lamp goes off. If
anything has gone working/input by then, the watcher harmlessly no-ops.
Each new idle event kills the prior watcher and arms a fresh one, so the
timer resets correctly.

## Turning the lamp off manually

Done for the day and don't want to wait 30 minutes? Run:

```bash
~/.claude/hue_hooks/hue_off.sh
```

This wipes the per-session state directory and turns the lamp off
immediately. The plain `off` priority is the lowest — any active session
would otherwise outvote it, so this command bypasses the priority system
entirely. (If you keep working in Claude after, the next hook event will
turn the lamp back on.)

## Pulse

Any status state can pulse instead of holding steady: add it to
`"pulse_states"` in the `tuning` block (e.g. `["working", "input"]`), or use
the per-state pulse chips in the dashboard. Hue's built-in breathe alert
stops after 15 seconds, so a tiny background pulser process (same pattern as
the sleep watcher, PID file at `/tmp/claude_hue_state/.pulse.json`) oscillates
the status lights' brightness until the state changes, then exits on its own.

## Transparent states ("keep scene")

Any status state can be transparent instead of a colour: set its tuning
payload to `{"transparent": true}` (or use the "◌ keep scene" chip in the
dashboard). Before claude-hue's first paint, the hook snapshots each status
light's state (`/tmp/claude_hue_state/.baseline.json`); a transparent state
restores that snapshot instead of painting and then drops it, so the next
takeover captures fresh. Example: idle transparent means your warm-white
scene comes back exactly whenever Claude stops, while working still paints
its colour. Transparent states never pulse, and match-status mode skips them.

## Context gauge (gradient lamps)

A Hue gradient lamp (e.g. Signe) can double as a context-window fuel gauge:
the bottom of the lamp fills red-orange with how much context the session has
used, the rest stays green for what's left. Recomputed on every hook event
from the most recent assistant message's token usage in the session
transcript, but only repainted when a segment actually changes — writing an
unchanged gradient makes the lamp blip on every tool call.

Add a `gauges` list (one entry per lamp) to `~/.claude/hue_hooks/config.json`:

```json
"gauges": [
  { "light_id_v1": 36, "light_id_v2": "<CLIP v2 UUID>", "points": 5, "reverse": false }
]
```

(The legacy single `gradient` block is still read if `gauges` is absent.)
Status targets are also lists now — any mix of `group_ids` (zones/rooms) and
`light_ids`. Gauge lamps outrank status: the dashboard writes a
`resolved_targets` block where any group containing a gauge lamp is expanded
to its member lights minus the gauge lamps, so status writes (and the flash
and pulser) never touch a gauge. Re-save from the dashboard after changing
group membership in the Hue app, since the expansion is computed at save
time.

Find the v2 UUID with:

```bash
curl -sk https://<bridge_ip>/clip/v2/resource/light \
  -H "hue-application-key: <username>" | python3 -m json.tool
```

(match `id_v1` to your light number). The context window size is
auto-detected per session: `[1m]` in the transcript's model id or in the
default model in `~/.claude/settings.json` means 1M, observed usage beyond
200k proves 1M, otherwise 200k. Set a top-level `"context_limit"` only to
force a specific value. Set `reverse` to true if the bar fills from the
wrong end. Colours are `GAUGE_USED_XY` /
`GAUGE_LEFT_XY` in `hue_hook.py`. Gradients need the CLIP v2 API, so the
gauge talks HTTPS to the bridge (self-signed cert, verification off).

The gauge lamp is independent of the status lights — don't include it in
`light_ids`/`group_id`, or every status change will wipe the gradient. With
multiple sessions the gauge shows whichever session fired a hook most
recently. It turns off together with the status lights (auto-sleep and
`hue_off.sh`).

With `"gauge_match_working": true` in the `tuning` block, the gauge drops the
fill bar and goes solid whenever any session is busy (`working`) or waiting
on a permission ask (`input`) — both lamps glow the same status colour, and
the bar returns once everything is idle.

## Notification handling

Claude Code's `Notification` event fires for two distinct reasons:

1. **Permission/decision requested** — *"Claude needs your permission to use Bash"*
2. **You've been idle ~60s** — *"Claude is waiting for your input"*

Only the first kind warrants `input` (purple). The second is just an idle
nudge, so we demote it to `idle` (amber) — otherwise every session you
finished and walked away from would latch the lamp purple.

## Customising colours

The constants at the top of `~/.claude/hue_hooks/hue_hook.py` (`STATES`,
`FLASH_COUNT`, `FLASH_GAP_SEC`, `IDLE_SLEEP_SEC`, `GAUGE_*_XY`) are defaults.
A `tuning` block in `config.json` overrides any of them per key — that's what
the [dashboard](#dashboard) writes, so colour and timing changes go live on
the next hook event without editing Python. Hue is 0–65535, sat
and bri are 0–254, and transitiontime is in 100ms units. See the
[Hue API v1 reference](https://developers.meethue.com/develop/hue-api/lights-api/)
for the full payload schema.

## Troubleshooting

Logs are appended to `/tmp/claude_hue.log`:

```bash
tail -f /tmp/claude_hue.log
```

The file grows unbounded. Truncate it occasionally with
`: > /tmp/claude_hue.log`, or set up `logrotate`.

If lights aren't responding:

1. Confirm the bridge is reachable: `curl http://<bridge_ip>/api/<username>/lights`
2. Re-run `setup.py` if you've changed networks or your bridge has a new IP
3. Check that `~/.claude/hue_hooks/config.json` exists and is readable

## Multiple sessions

Run more than one Claude Code session at a time? The lamp does the right
thing. Each session's state is tracked in `/tmp/claude_hue_state/<id>.json`,
and the lamp shows the LOUDEST active state across all sessions:

```
input > working > idle > off
```

So if session A is working and session B asks for permission, the lamp goes
purple. When B's permission is resolved, the lamp returns to blue (A is still
working). Only when ALL sessions go idle does the lamp go amber. A crashed
session that never fires `SessionEnd` is forgotten after 30 minutes of
inactivity (`STATE_TTL_SEC` in `hue_hook.py`).

Session IDs are read from Claude Code's hook payload (canonical), with a
fallback to the `CLAUDE_SESSION_ID` env var, and a final fallback to
`"default"` for manual command-line invocations.

## How it works

Each Claude Code hook event invokes `hue_hook.sh <state>`. The shell wrapper
captures the JSON payload from stdin (which carries the session ID), then
fires `hue_hook.py` in the background and exits immediately so Claude is
never blocked. The Python script:

1. Loads `~/.claude/hue_hooks/config.json`
2. Writes the calling session's state to `/tmp/claude_hue_state/<id>.json` (atomic rename)
3. Scans all session files, prunes stale entries, picks the loudest active state
4. PUTs that state's payload to the Hue Bridge's v1 REST API

Errors at every step are logged but never propagate.

No always-on daemon, no third-party deps, no cloud — just stdlib HTTP to
your local bridge. The auto-sleep watcher is the one exception: a tiny
self-terminating background process that exists for at most 30 minutes per
idle event.
