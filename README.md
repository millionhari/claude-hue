# claude-hue dashboard

Local web console for tuning the [claude-hue](../claude-lamp-philips-hue) lamp
integration — status colors, brightness, finish flash, context gauge, sleep
timing, and which lamps to drive.

## Run

```bash
python3 dashboard.py        # → http://127.0.0.1:8420
```

Stdlib only, binds to localhost. Edits are written to
`~/.claude/hue_hooks/config.json` under a `tuning` block that `hue_hook.py`
reads on every hook event — saving is live immediately, nothing to restart.

Preview buttons push draft values straight to the lamps without saving;
the next real hook event repaints whatever the actual state is.
