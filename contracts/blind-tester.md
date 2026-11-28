# blind-tester (blind spec 3.1, row 601)
schema: blind_tests.v1
verbs:
- gets: the card (title, acceptance criteria, cited rows, spec section text), `card_hash`, the interface snapshot, the harness API; on a rerun, the red-check result (names, base classification).
- sees: the acceptance text and the interface snapshot only; never the diff, source, implementer report, reviewer findings, or other cards' tests.
- done-when: every numbered acceptance statement has at least one test; every test has an `examples.jsonl` row with a named source; tests drive the product only through the harness API; `HEADER.json` carries the `card_hash`; every test is named by its full id, `<card id>.<test method name>`, as the harness runs it (example: `t_5219f8cf.test_greets`), in `examples.jsonl` and in `covered`, never by the bare method name. Test names are unique within a card: a name used twice fails the check.
- reports: `blind_tests.v1`: statements covered; statements not testable and why (these become `not_run` reasons); a question for the guide when the acceptance text is ambiguous.
- may-not: C; read any path outside `/workspace` and `/interface`; name a file, function or module absent from the interface snapshot; write anything but `cards/<id>/**`; weaken or delete an existing test.
- may-not: never bypass a Zenkai Rule (docs/ZENKAI.md) without an override that names it.
