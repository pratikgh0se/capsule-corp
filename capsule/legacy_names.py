"""The retired old system's name, where it may still appear (owner decision D270).

D198/D202 retired the old board system; D270 says its name may live only in stored history. Every
literal below is read from disk, from a database row, from an owner environment or from an old
file, so renaming it would break the owner's machine or orphan history. Live code imports these
constants instead of spelling the name, which keeps the name in this one file. The public lint
exempts exactly this file (release/private-denylist.txt, "D270 stored history").

Each constant says what stores it. Shell code that needs a kept path defines it once, in the
script, from this module's value.
"""

NAME = "hermes"

# Category of the old name in release/private-denylist.txt (retirement guards read it there).
DENYLIST_CATEGORY = NAME

# The old system's home folder under $HOME. The deployed park script and old boards may still
# sit there (read only).
LEGACY_HOME_DIRNAME = "." + NAME

# Environment name only the old code honoured (the oracle tests and the temp-home guard set it).
OLD_ENV_HOME = NAME.upper() + "_HOME"   # honoured by the old code only

# Variables the old CLI read from the environment; a quality hook strips both generations.
OLD_KANBAN_ENV_PREFIX = NAME.upper() + "_KANBAN_"
OLD_KANBAN_ENV_SUFFIXES = ("DB", "BOARD", "TASK", "RUN_ID", "CLAIM_LOCK", "WORKSPACES_ROOT")
OLD_KANBAN_ENV_NAMES = tuple(OLD_KANBAN_ENV_PREFIX + s for s in OLD_KANBAN_ENV_SUFFIXES)
NEW_KANBAN_ENV_PREFIX = "LEGACY_KANBAN_"
NEW_KANBAN_ENV_NAMES = tuple(NEW_KANBAN_ENV_PREFIX + s for s in OLD_KANBAN_ENV_SUFFIXES)

# The old code's importable package (used only by the oracle tests that run the old repo).
LEGACY_CLI_PKG = NAME + "_cli"

# Key written into stored ledger rows and part of their dedupe key (P13).
LEDGER_EVENT_KEY = NAME + "_event_id"

# Stored `era` value in Postgres rows, spool files and dashboards; also the `from` field of
# old spooled records.
ERA_VALUE = NAME

# Stored run ids of the old era ("<prefix><task id>") and its payload key.
RUN_ID_PREFIX = NAME + "-"

# Launchd labels of the old gateways under ~/Library/LaunchAgents.
LAUNCHD_LABEL_PREFIX = "ai." + NAME + "."

# Old decision-log text mentions the system by name; the messaging classifier still matches it.
CLASSIFIER_WORD = NAME

# Stored text of rows already written when stale old-era cards were closed.
STALE_CLOSE_REASON = NAME + " era, superseded by capsule"

# What the dark test scans old logs for: the old home folder, its home variable, its send verb and
# the bare word.
MENTION_PATTERN = (r"\." + NAME + "|" + NAME.upper() + r"_HOME|" + NAME + r" send|\b" + NAME
                   + r"\b")
