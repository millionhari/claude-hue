#!/bin/bash
STATE="${1:-idle}"
DIR="$(cd "$(dirname "$0")" && pwd)"

# Capture stdin synchronously — Claude Code passes a JSON payload (with
# session_id) on stdin. Reading it here means the backgrounded Python child
# sees the same data even after the parent shell exits.
STDIN_DATA=""
if [ ! -t 0 ]; then
  STDIN_DATA="$(cat 2>/dev/null || true)"
fi

nohup python3 "$DIR/hue_hook.py" "$STATE" <<<"$STDIN_DATA" >> /tmp/claude_hue.log 2>&1 &
disown
exit 0
