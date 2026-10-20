"""One JSON line per event on stdout; Container Apps ships this to Log Analytics."""
from __future__ import annotations

import json
import sys
import time
from collections import deque

# Recent per-question records, for the admin page when Log Analytics isn't configured (local runs).
RECENT_TURNS: deque[dict] = deque(maxlen=1000)


def log_event(event: str, **fields) -> None:
    rec = {"ts": round(time.time(), 3), "event": event, **fields}
    if event == "agent_turn":
        RECENT_TURNS.append(rec)
    sys.stdout.write(json.dumps(rec, default=str) + "\n")
    sys.stdout.flush()
