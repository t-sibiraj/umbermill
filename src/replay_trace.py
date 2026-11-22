"""Optional structured replay diagnostics for comparing selector decisions."""

import json
import os
import sys


def trace_decision(event, **details):
    if os.environ.get("UMBERMILL_REPLAY_TRACE") == "2":
        print(json.dumps({"event": event, **details}, separators=(",", ":")), file=sys.stderr, flush=True)
