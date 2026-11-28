# docs/licenses

`declared.json` is the one-time human decision for a component `notice_gen.py`
cannot classify from its own metadata (typically a Dockerfile pin, which
carries no licence at all): a name maps to an SPDX id, decided once by a
person and enforced forever after by the gate. It only fills in an `unknown`
licence - it can never override a licence a component actually declares, so a
human note can't launder a GPL dependency into MIT.

`hypothesis` is declared `MPL-2.0`, which classifies **allowed** under D240 (see `POLICY.md`:
LGPL-*/MPL-* allowed, GPL-*/AGPL-* denied in shipped code, dev-only tools in `dev-only.txt` exempt).
It is a weak-copyleft test-time dependency inside the worker image, a separate program and not part of Capsule's shipped code.

`pytest` (MIT) and `mutmut` (BSD-3-Clause) were verified on 2026-10-24 against
each project's own `LICENSE` at the pinned tag (`pytest-dev/pytest` 8.3.3,
`boxed/mutmut` 3.7.0) and declared above.

`components/` holds each such project's own licence file verbatim (copyright line
included, so a third party's reserved-rights notice appears there and nowhere
else). `texts/MPL-2.0.txt` is Mozilla's official text
(https://www.mozilla.org/media/MPL/2.0/index.815ca599c9df.txt). `hypothesis` is
installed unmodified from PyPI inside the worker image (no vendored copy exists
in this repository), which is what keeps MPL-2.0 file-level obligations at
"notice only".

`texts/{MIT,ISC,BSD-3-Clause}.txt` are the canonical SPDX plain-text
templates verbatim, including their `<year>` / `<copyright holder>`
placeholders - those markers are part of the licence text itself and are
never filled in with a real name here, since that would misstate whose
copyright a reproduced third-party licence covers.

`*.pip-licenses.json` are `pip-licenses --format=json` output (v5.5.5, Python 3.13) from a throwaway venv built from each
image's Dockerfile pins (`python-layer` = `docker/stacks/python`, `web-layer` = the `/opt/playwright` venv). Regenerate
them when a pin changes; `notice_gen.py --scan` reports a pin that differs from the JSON.

`notice_gen.py --scan` also reports names the repo uses but `NOTICE.md` does not mention (compose images in
`observability/*.yml`, workflow `uses:`, release-URL downloads, every `name==version` pin in the workflows, Brewfile,
bootstrap.lock brew lists, every `declared.json` key). A name counts as mentioned when it appears in NOTICE as a whole
word, ignoring case and punctuation (`uv` does not match `uvicorn`). Not covered: pipx/uvx/npx, downloads from hosts
other than github.com, compose files outside `observability/`, and the Dockerfile `FROM` images. The notes-vault clone
in bootstrap.lock is a host tool and is deliberately not scanned.
`--check` fails when a tracked text file under capsule, maintenance, bin, docker or observability (not tests) says in its
first 15 lines "lifted / adapted / copied / vendored / ported from <owner>/<repo>", or when anything under
`capsule/licenses/` is not named by path in NOTICE; a provenance header further down a file, or worded another way,
is not seen. It also fails when the newest `SCAN-YYYY-MM-DD.md` is over 90 days old (refresh steps: RUNBOOK "Legal
limits and money"). A `declared.json` value of `source-available` records a component whose
source is public but whose licence is not open source; it must still be named in NOTICE.
