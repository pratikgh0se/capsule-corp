---
description: List what in the Capsule factory is waiting on you (read-only)
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/capsule_chat.py needs-you)
---

What is waiting on the user, read just now:

!`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/capsule_chat.py needs-you`

Show the list above to the user exactly as it is. If it is not empty, offer to look closer at one
item, but do not act on any of them, and do not run anything else or change anything.
