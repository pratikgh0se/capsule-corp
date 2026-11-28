"""Parser arguments that only some `incidents` verbs take: `row`, `landed` and `close` (light:
argparse only, so the CLI builds its parser without the incident code)."""


def _row(value):
    return value if value == "auto" else int(value)


def add(ip, name):
    if name == "row":
        ip.add_argument("row", type=_row, help="a row number, or auto: add the draft row")
    if name == "landed":
        ip.add_argument("sha", nargs="?", default=None, help="the main commit that holds the fix")
        ip.add_argument("--sha", dest="sha_flag", default=None, help="same, as a flag")
        ip.add_argument("--test", default=None, help="also set this recurrence test (retest)")
        ip.add_argument("--renamed-from", default=None, metavar="OLD_TEST_ID",
                        help="the recurrence test was renamed after the fix: its old id, which the"
                             " fix commit holds")
        ip.add_argument("--row", choices=["auto"], default=None, help="also add the draft row")
    if name == "close":
        ip.add_argument("--quiet", action="store_true", help="queue no 'incident closed' notice")
