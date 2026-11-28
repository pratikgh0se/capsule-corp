"""D229 (Z2 second oracle): Telegram is retired and Slack is the one channel. A case-insensitive
scan of the live code paths must match only this explicit allowlist, so a Telegram path cannot
creep back in unnoticed. Adding a file or a higher count here is a decision, not a convenience."""
import re
import unittest
from pathlib import Path

from capsule import paths
from capsule.tests.public_tree import private_only

# Live code paths. Tests, fixtures and historical docs (docs/, CHANGELOG, SETUP.md) are not
# scanned: they describe or exercise the retirement.
SCAN = ("capsule", "maintenance", "bin", "launchd", "observability", "deploy.sh", "bootstrap.sh")
SKIP_PARTS = {"tests", "fixtures", "__pycache__"}
TEXT_SUFFIXES = {".py", ".sh", ".lua", ".zsh", ".json", ".yaml", ".yml", ".template", ".md",
                 ".plist", ""}

# file -> (max matching lines, why it stays)
ALLOWED = {
    # redaction and classification of old logs and backups: these must keep recognising it
    "maintenance/scrub.py": (4, "token redaction for old logs and backups (PROTECTED)"),
    "observability/fluent-bit/scrub.lua": (10, "token redaction mirror of scrub.py"),
    "maintenance/decision_log.py": (1, "decision classifier keyword"),
    # gated-off legacy-era secrets importer, removed with the legacy sweep
    "maintenance/secrets_import.py": (7, "one-time secrets importer"),
    # the identity name that the Slack sender and listener read tokens through (D132)
    "capsule/config.capsule-corp.example.json": (1, "identity telegram-gateway, user renames"),
    # legacy gateway health probe in heal.sh, removed with the legacy sweep
    "maintenance/heal.sh": (2, "comments noting the legacy gateway probe is gone"),
    # the retirement refusals themselves
    "deploy.sh": (2, "AGENT_OPS_CHAT=telegram exits 2 with 'retired (D229)'"),
    "bootstrap.sh": (1, "--chat telegram exits 2 with 'retired (D229)'"),
    # retirement notes and old-history comments
    "maintenance/board_bridge.py": (1, "log sender note"),
    "maintenance/run.sh": (3, "comments recording why per-job alerts were removed"),
    "maintenance/report.sh": (3, "comments about wikilinks and muted alerts"),
    "maintenance/security.md": (1, "threat-model prose"),
    "maintenance/setup-semantic.sh": (1, "comment"),
    "maintenance/usage_guard.py": (1, "comment"),
    "maintenance/vault_git.py": (1, "comment"),
    "observability/grafana/render_alert_policy.py": (1, "docstring: Telegram is retired"),
}


# Live docs: RUNBOOK and EGRESS and STATUS may mention Telegram only as history or retirement.
DOCS = ("docs/RUNBOOK.md", "docs/EGRESS.md", "STATUS.md")
DOC_ALLOWED = {
    "docs/RUNBOOK.md": (72, "history paragraphs, cutover/secrets-import steps, retirement notes"),
    "docs/EGRESS.md": (5, "update banner and the retired-door notes"),
    "STATUS.md": (1, "the line recording the retirement"),
}
# Stale live instructions that must never come back (flags and names that no longer exist).
FORBIDDEN_DOC_TEXT = ("OPS_ALERT_SLACK=1", "OPS_ALERT_SLACK=0", "_default_telegram_sender",
                      "TELEGRAM_ONLY_FILE", "`${OPS_TG_BOT_TOKEN}`")


def _scan():
    found = {}
    for entry in SCAN:
        root = paths.REPO / entry
        files = [root] if root.is_file() else sorted(root.rglob("*")) if root.is_dir() else []
        for f in files:
            rel = f.relative_to(paths.REPO)
            if not f.is_file() or SKIP_PARTS & set(rel.parts) or f.suffix not in TEXT_SUFFIXES:
                continue
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            n = sum(1 for ln in lines if re.search("telegram", ln, re.I))
            if n:
                found[str(rel)] = n
    return found


class TelegramRetired(unittest.TestCase):
    def test_live_code_mentions_telegram_only_where_allowlisted(self):
        found = _scan()
        extra = {f: n for f, n in found.items() if f not in ALLOWED or n > ALLOWED[f][0]}
        self.assertEqual(extra, {}, "Telegram is retired (D229): add a reason to ALLOWED")

    @private_only
    def test_allowlist_has_no_stale_entries(self):
        found = _scan()
        stale = sorted(f for f in ALLOWED if f not in found)
        self.assertEqual(stale, [], "remove allowlist entries whose mentions are gone")

    @private_only
    def test_live_docs_mention_telegram_only_within_their_budget(self):
        for rel, (cap, _why) in DOC_ALLOWED.items():
            text = (paths.REPO / rel).read_text(encoding="utf-8")
            n = sum(1 for ln in text.splitlines() if re.search("telegram", ln, re.I))
            self.assertLessEqual(n, cap, f"{rel}: {n} Telegram lines, budget {cap}")

    @private_only
    def test_live_docs_carry_no_stale_flag_or_name_instructions(self):
        for rel in DOCS:
            text = (paths.REPO / rel).read_text(encoding="utf-8")
            for bad in FORBIDDEN_DOC_TEXT:
                self.assertNotIn(bad, text, f"{rel} still says {bad}")

    def test_the_deleted_telegram_files_stay_deleted(self):
        for rel in ("maintenance/topics.py", "bin/open-brainstorm.sh", "bin/close-brainstorm.sh",
                    "observability/grafana/provisioning/alerting/contact-points.yaml"):
            self.assertFalse((paths.REPO / rel).exists(), rel)

    def test_chat_switch_default_is_slack_never_empty(self):
        src = (paths.REPO / "maintenance" / "board_bridge.py").read_text(encoding="utf-8")
        self.assertNotIn('"OPS_CHAT_PROFILE", "telegram"', src)
        self.assertNotIn("_default_telegram_sender", src)


if __name__ == "__main__":
    unittest.main()
