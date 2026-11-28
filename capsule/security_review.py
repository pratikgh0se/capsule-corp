"""BLD-02: the security reviewer, a fourth blocking review behind `redesign.security_review`.
Off: `roles` is the three reviews of today. On: `security` joins them, routed by
`routing.security`, and reads only the diff, the manifest part of it and the card's risk class
(row 366), never another review's verdict."""
import json
import re

from capsule import bundles, config, contracts, workspace

ROLE = "security"
BASE = ("validator", "reviewer", "guide")
MAX_PART = 200 * 1024     # a share of the review packet cap, for each of the two diffs
# dependency files, lockfiles, image files and CI files, by base name or by folder
MANIFEST = re.compile(
    r"(^|/)(package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|npm-shrinkwrap\.json|bun\.lockb?"
    r"|requirements[^/]*\.txt|pyproject\.toml|poetry\.lock|uv\.lock|Pipfile(\.lock)?"
    r"|setup\.(py|cfg)"
    r"|go\.(mod|sum)|Cargo\.(toml|lock)|Gemfile(\.lock)?|composer\.(json|lock)|renovate\.json5?"
    r"|capsule\.toml|(Dockerfile|Containerfile)[^/]*|[^/]*\.dockerfile|[^/]*compose[^/]*\.ya?ml"
    r"|images\.lock|\.gitlab-ci\.yml|Jenkinsfile|azure-pipelines\.ya?ml|bitbucket-pipelines\.ya?ml"
    r"|pom\.xml|build\.gradle(\.kts)?|gradle\.properties|gradle-wrapper\.(properties|jar)"
    r"|[^/]*\.gemspec|\.npmrc|\.yarnrc(\.yml)?|[^/]*\.csproj|packages\.lock\.json"
    r"|[Nn]u[Gg]et\.[Cc]onfig)$|(^|/)\.(github|circleci|buildkite)/")


def roles(cfg):
    """The review roles of this board's round."""
    return BASE + (ROLE,) if config.redesign(cfg)["security_review"] else BASE


def _cut(text):
    text = text or ""
    return text[:MAX_PART] + ("\n(cut)" if len(text) > MAX_PART else "")


def _manifest_diff(diff):
    """The `diff --git` blocks of `diff` that touch a manifest path."""
    blocks = re.split(r"(?m)^(?=diff --git )", diff or "")
    named = ((re.match(r"diff --git a/(\S+)", b), b) for b in blocks)
    return "".join(b for m, b in named if m and MANIFEST.search(m[1]))


def inputs(task, ws, base):
    """The security run's job inputs: the diff, its manifest part and the card's risk flags."""
    diff = workspace.diff(ws, base)
    flags = ", ".join(json.loads(task["risk_flags"] or "[]")) or "none"
    return {"diff": diff, "dependency_manifest_diff": _manifest_diff(diff),
            "risk_class": "Risk flags: " + flags}


def bundle(task, got, retry_error=None):
    """The security run's prompt, built from `inputs` and nothing else."""
    parts = [bundles._header(task),
             str(got.get("risk_class") or "Risk flags: none"),
             "## Dependency and manifest diff\n```diff\n"
             + (_cut(got.get("dependency_manifest_diff")) or "(none)") + "\n```",
             "## Diff\n```diff\n" + _cut(got.get("diff")) + "\n```"]
    if note := bundles._invalid_document_section(retry_error):
        parts.insert(1, note)
    parts.append(bundles._envelope(ROLE, task))
    return {"system": contracts.render(ROLE), "prompt": "\n\n".join(parts)}
