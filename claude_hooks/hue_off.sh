#!/bin/bash
# Force the lamp off and clear all session state. Run when you're done for
# the day — bypasses the loudest-wins priority so stragglers can't outvote.
DIR="$(cd "$(dirname "$0")" && pwd)"
nohup python3 "$DIR/hue_hook.py" off-all >> /tmp/claude_hue.log 2>&1 &
disown
exit 0
