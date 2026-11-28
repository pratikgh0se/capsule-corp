# align: planning sitting for one project (design §3.1-3.3, rows 349, 354-356, 360, 414, 419)

Any harness (Claude Code, Codex, Cursor, OpenCode, Pi) runs this prompt to plan a project with
the owner. A sitting ends with a signed plan that passes `capsule plan check`. Then
`capsule plan cut` files the plan's cards on the project's board.

## Running Capsule commands
- Below, `capsule` means the launcher `~/.local/bin/capsule`, which `capsule install` writes.
- If that file is missing, run `python3 -m capsule` (Python 3.11+) from the Capsule repo. It is
  at `$CAPSULE_REPO`, otherwise `~/CodeBase/capsule-corp`.
- The board is the project's Capsule board. Its name is in the umbrella note; if not, ask
  the owner. A new project gets one with `capsule board new <name> --repo <path>`.

## Start: five steps
1. Find the vault: `capsule host get vault.path` (it honours the vault env var).
2. Read the resume note `Projects/<project> - Alignment.md`. If it is missing, create it from
   the template below.
3. Read the umbrella note `Projects/<project>.md`, if it exists.
4. Read every decision row the resume note cites.
5. Ask the first question in the note's "Open questions" queue.

## Loop: one question at a time
- Ask one question. Put your recommendation first, then 2-4 options in plain words.
- The owner answers. Record each decision as a numbered row in the project's decision log: host
  setting `vault.decision_notes` (a glob; `host.decision_note` resolves it), otherwise the umbrella note. Cite the row.
- After each exchange, update all four sections of the resume note.
- A theme closes when three questions in a row produce no new decision (row 349).
- Show a diagram or build a throwaway POC when it answers a question faster than words.

## Outputs (existing forms only, row 360)
1. Spec: `docs/superpowers/specs/<YYYY-MM-DD>-<topic>-design.md` in the project repo. It
   carries:
   - at least one mermaid diagram;
   - after the owner approves the diagrams, the line `Diagrams approved: <YYYY-MM-DD>`;
   - one line `Unknown: <name>` per open technical unknown. `<name>` is one branch-safe token
     (letters, digits, `-`), identical in `Unknown:`, `POC <name>:` and `poc/<name>`;
   - per unknown, a never-merged branch `poc/<name>` and the line
     `POC <name>: <findings note path>`;
   - after the owner confirms the research personally, the line `Research validated: <YYYY-MM-DD>`,
     or `Research: none needed` if he says so.
2. Plan: `docs/superpowers/plans/<YYYY-MM-DD>-<topic>.md`, in the format below.
3. Gate: run `capsule plan check <plan>` and fix every item it names.
4. Cards: after the owner says go, run `capsule plan cut <plan> --board <board>`.

## Plan format (what `capsule plan cut` reads)
```
Spec: ../specs/<YYYY-MM-DD>-<topic>-design.md
Epic: <epic name>
Signed: <owner name> <YYYY-MM-DD>

### C01 - <card title>
Files: path/a.py, path/b_test.py
Check: <one shell command that proves the card works>
Expect: <text the check prints on success>
Size: S
Risk: <flags, optional>
Deps: <card IDs, optional>
Cites: §<section>, rows <n>, <m>
<body: what to build and why, in plain steps>
```
- Every card needs `Files:` and `Check:`: the declared file set (row 400) and the validation
  contract (row 356).
- Keep cards small: one reviewable change each.

## Resume note template (`Projects/<project> - Alignment.md`)
```
## Spec draft
<repo spec path> | <vault mirror path>
## Decision rows so far
- <row>: <one line>
## Open questions
1. <next question first>
## Last exchange
<question> / <answer> / <row>
```

- may-not: write `Signed:`, `Diagrams approved:` or `Research validated:` unless the owner says so in
  this sitting; merge a `poc/` branch; edit product code or a contract file; skip the gate.
- may-not: never bypass a Zenkai Rule (docs/ZENKAI.md) without an override that names it.
