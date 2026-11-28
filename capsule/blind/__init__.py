"""Blind validator tests (blind spec docs: vault Specs "Blind validator test repo", rows 601/602).

Acceptance tests live in a per-board sibling repo that only the blind-tester role writes; the tick
runs them against a read-only copy of the card's work and only results and examples flow back
(D15-D18). This package holds the host-side pieces: `init` (create the repo and its board config),
`preflight` (is the repo usable and outside every mount), `cli` (the `capsule blind` verbs). The
in-container harness API lives in `harness/`, a protected path (`workspace.PROTECTED`)."""
