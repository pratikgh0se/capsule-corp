---
slug: <slug>
date: YYYY-MM-DD
appetite_cards: <int>
appetite_epic_runs: <int>
appetite_window_pct: <int>
one_number: <the single measurable outcome>
instrument: <the existing thing that measures it>
kill_by: YYYY-MM-DD
witnesses: [<id>, <id>]
distribution: <channel>
distribution_fee: <x% or none>
compliance_owner: <name>
compliance_clock_checked: yes|no
---

## For future agent

A bet, in Shape Up's shape plus one number and a kill line. Copy this file to
`Pitches/<slug>.md`, fill every frontmatter key, and keep it lint-clean: the
frontmatter above is exactly what `pitch_lint.py` (M247/M248, parked on "first
bet stamped") will check, so the fields exist in the first pitch and the lint
has data the day it ships. The appetite is the circuit breaker - a bet that
does not ship inside it returns to the table to be re-shaped, not extended.

## Problem

<A behaviour someone is failing at, not a capability the software lacks. Cite a
witness id that exists under `## Witnesses` in `Boards/Order Book.md`.>

## Appetite

<A number first, in Capsule's real units: cards, epic-lane runs (row 133's
`lane: epic`), and percentage of the weekly window. Hours are meaningless when
the constraint is a weekly allowance.>

## Solution sketch

<Enough to size it. Not a design.>

## Rabbit holes

<The parts that could eat the appetite, named so they are avoided on purpose.>

## No-gos

<At least one thing this bet will NOT do. Shape Up's scope defence, and the
cheapest guard against an agent expanding scope tenaciously (row 110).>

## One number

<One measurable outcome, and the instrument that measures it - which must
already exist, or building it is inside the appetite.>

## Kill criteria

<What result, by what date, ends this. Written BEFORE the bet. The date goes in
`kill_by` and into the `## Live bet` line; a passed kill date with no verdict
blocks the next bet.>

## Distribution and compliance

<Which channel, at which fee, with the compliance owner named - a bet with no
channel is a hobby, and this is the field that would have caught a past video-tool project.
Say whether shipping starts the EU CRA reporting clock or an AI Act Article 50
duty; set `compliance_clock_checked` accordingly.>
