---
description: Show what the Capsule factory is doing right now (read-only)
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/capsule_chat.py status)
---

Capsule status, read just now:

!`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/capsule_chat.py status`

Show the text above to the user exactly as it is, then add at most one plain sentence on what
stands out (for example a loop that is down, or something waiting on them). Do not run anything
else, do not change anything, and do not add ids or file paths.
