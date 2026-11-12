#!/usr/bin/env bash
# UserPromptSubmit hook: appends each genuine user prompt to docs/ai-log.md.
# The Comity write-up asks for the prompts used; this records them verbatim as they are sent.
# Skipped: messages from Claude's own helper agents and background-task notices, which arrive
# through the same hook but are not the user's words. Stripped: editor context blocks
# (e.g. <ide_opened_file>) that the IDE prepends to a real prompt.
set -euo pipefail
root="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
log="$root/docs/ai-log.md"
mkdir -p "$(dirname "$log")"
python3 -c '
import datetime, json, re, sys
d = json.load(sys.stdin)
prompt = d.get("prompt") or ""
prompt = re.sub(r"<(ide_opened_file|ide_selection)>.*?</\1>", "", prompt, flags=re.S).strip()
not_user = ("<agent-message", "<task-notification", "<system-reminder",
            "Another Claude session sent a message", "[SYSTEM NOTIFICATION")
if not prompt or prompt.startswith(not_user):
    sys.exit(0)
ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
sid = (d.get("session_id") or "unknown")[:8]
quoted = "\n".join("> " + line if line else ">" for line in prompt.splitlines())
with open(sys.argv[1], "a") as f:
    f.write(f"\n### {ts} · session {sid}\n\n{quoted}\n")
' "$log"
