"""Seatbelt profile renderer for the light runner (rows 373, 378, 395). Test names follow the
intent rows of contracts/intent/light_runner.jsonl. The static class pins the rendered text;
the live class is the independent second oracle (Z2): the real `sandbox-exec` in a temp dir.
Live cases fail loudly on macOS when `sandbox-exec` cannot apply (Z4) and skip with a reason
elsewhere (the light tier is unavailable there)."""
import http.server
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from capsule import workspace
from capsule.runners import seatbelt

HOME = "/srv/example"

# The golden profile, one list item per line; {WT}/{VAULT} stand for the fixture's real paths
# ({WTRE} is the worktree escaped for a regex: a real path may contain a dot).
_DENY_W = '(deny file-write* (regex #"^{WTRE}/'
GOLDEN = "\n".join([
    "(version 1)",
    ";; Capsule light runner (rows 373, 378, 395). Rendered by capsule/runners/seatbelt.py;"
    " never hand-edit.",
    "(deny default)",
    ";; process basics, and the read-only system paths a process needs to start",
    "(allow process-exec* process-fork)",
    "(allow signal (target self))",
    "(allow sysctl-read)",
    "(allow file-read-metadata)",
    '(allow file-read-data (literal "/"))',
    '(allow file-read* (subpath "/usr") (subpath "/bin") (subpath "/sbin") (subpath "/System")'
    ' (subpath "/Library") (subpath "/private/etc") (subpath "/dev")'
    ' (subpath "/private/var/db/dyld") (subpath "/private/var/db/timezone"))',
    '(allow file-write* (literal "/dev/null") (literal "/dev/dtracehelper"))',
    ";; the worktree (its scratch dir is inside it)",
    '(allow file-read* file-write* (subpath "{WT}"))',
    ";; vault read scope",
    '(allow file-read* (subpath "{VAULT}"))',
    ";; network (row 373): only these endpoints",
    '(allow network-outbound (remote tcp "localhost:8080"))',
    ";; denies come last: same-operation allows above lose to them (row 395 layer 1)",
    _DENY_W + '[cC][oO][nN][tT][rR][aA][cC][tT][sS](/.*)?$"))',
    _DENY_W + r'[dD][oO][cC][sS]/[zZ][eE][nN][kK][aA][iI]\.[mM][dD]$"))',
    _DENY_W + r'(.*/)?[sS][oO][uU][lL].*\.[mM][dD]$"))',
    _DENY_W + r'(.*/)?[cC][lL][aA][uU][dD][eE]\.[mM][dD]$"))',
    _DENY_W + r'(.*/)?[aA][gG][eE][nN][tT][sS]\.[mM][dD]$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[iI][nN][tT][eE][nN][tT]\.[pP][yY]$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[wW][oO][rR][kK][sS][pP][aA][cC][eE]\.[pP][yY]$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[lL][aA][nN][dD][eE][rR]\.[pP][yY]$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[tT][eE][sS][tT]_[sS][hH][aA][rR][dD].*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[sS][hH][aA][rR][dD][sS]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[uU][nN][bB][lL][oO][cC][kK][eE][dD]_[sS][tT][oO][pP]'
    r'\.[pP][yY]$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[eE][nN][gG][iI][nN][eE]_[hH][eE][aA][lL]'
    r'\.[pP][yY]$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[sS][aA][fF][eE]_[wW][rR][iI][tT][eE]\.[pP][yY]$"))',
    _DENY_W + r'[dD][oO][cC][kK][eE][rR]/[bB][aA][sS][eE](/.*)?$"))',
    _DENY_W + r'[dD][oO][cC][kK][eE][rR]/[sS][tT][aA][cC][kK][sS](/.*)?$"))',
    _DENY_W + r'[dD][oO][cC][kK][eE][rR]/[iI][mM][aA][gG][eE][sS]\.[lL][oO][cC][kK]$"))',
    _DENY_W + r'[dD][oO][cC][kK][eE][rR]/[bB][uU][iI][lL][dD]-[bB][aA][sS][eE]\.[sS][hH]$"))',
    _DENY_W + r'[dD][oO][cC][kK][eE][rR]/[bB][uU][iI][lL][dD]_[mM][iI][nN]_[fF][rR][eE][eE]_'
    r'[bB][yY][tT][eE][sS]$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[iI][mM][aA][gG][eE]_[rR][eE][nN][dD][eE][rR]'
    r'\.[pP][yY]$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]\.[tT][oO][mM][lL]$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[bB][lL][iI][nN][dD]/[hH][aA][rR][nN][eE][sS][sS]'
    r'(/.*)?$"))',
    _DENY_W + r'[mM][aA][iI][nN][tT][eE][nN][aA][nN][cC][eE]/[mM][eE][tT][eE][rR][sS]'
    r'\.[pP][yY]$"))',
    _DENY_W + r'[cC][aA][lL][iI][bB][rR][aA][tT][iI][oO][nN](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[iI][nN][tT][eE][rR][rR][uU][pP][tT][sS]'
    r'\.[jJ][sS][oO][nN]$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[bB][lL][iI][nN][dD]/[rR][eE][dD]\.[pP][yY]$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[bB][lL][iI][nN][dD]/[lL][iI][nN][tT]\.[pP][yY]$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[bB][lL][iI][nN][dD]/[sS][eE][tT][tT][lL][eE]'
    r'\.[pP][yY]$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[lL][aA][nN][dD][eE][rR]_[bB][lL][iI][nN][dD]'
    r'\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[lL][aA][nN][dD][eE][rR]_[bB][lL][iI][nN][dD]'
    r'(/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[bB][lL][iI][nN][dD]/[rR][uU][nN]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[bB][lL][iI][nN][dD]/[rR][uU][nN](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[bB][lL][iI][nN][dD]/[gG][aA][tT][eE]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[bB][lL][iI][nN][dD]/[gG][aA][tT][eE](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[bB][lL][iI][nN][dD]/[pP][rR][eE][fF][lL][iI]'
    r'[gG][hH][tT]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[bB][lL][iI][nN][dD]/[pP][rR][eE][fF][lL][iI]'
    r'[gG][hH][tT](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[iI][nN][cC][iI][dD][eE][nN][tT]_[bB][lL][iI]'
    r'[nN][dD]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[iI][nN][cC][iI][dD][eE][nN][tT]_[bB][lL][iI]'
    r'[nN][dD](/.*)?$"))',
    _DENY_W + r'[tT][eE][sS][tT][sS]/[mM][uU][tT][aA][tT][iI][oO][nN]_[cC][hH][eE][cC][kK]'
    r'\.[pP][yY]$"))',
    _DENY_W + r'[tT][eE][sS][tT][sS]/[sS][iI][zZ][eE]_[cC][hH][eE][cC][kK]\.[pP][yY]$"))',
    _DENY_W + r'[tT][eE][sS][tT][sS]/[sS][iI][mM][pP][lL][iI][cC][iI][tT][yY]_[cC][hH][eE][cC]'
    r'[kK]\.[pP][yY]$"))',
    _DENY_W + r'[tT][eE][sS][tT][sS]/[rR][uU][fF][fF]_[gG][aA][tT][eE]\.[pP][yY]$"))',
    _DENY_W + r'[tT][eE][sS][tT][sS]/[pP][oO][rR][tT][aA][bB][iI][lL][iI][tT][yY]_[lL][iI]'
    r'[nN][tT]\.[pP][yY]$"))',
    _DENY_W + r'[tT][eE][sS][tT][sS]/[sS][iI][zZ][eE]_[bB][aA][sS][eE][lL][iI][nN][eE]'
    r'\.[pP][yY]$"))',
    _DENY_W + r'[tT][eE][sS][tT][sS]/[sS][iI][zZ][eE]_[bB][aA][sS][eE][lL][iI][nN][eE]'
    r'\.[jJ][sS][oO][nN]$"))',
    _DENY_W + r'[tT][eE][sS][tT][sS]/[dD][iI][sS][cC][oO][vV][eE][rR]_[tT][mM][pP]'
    r'\.[pP][yY]$"))',
    _DENY_W + r'[tT][eE][sS][tT][sS]/[mM][uU][tT][aA][tT][iI][oO][nN]_[nN][eE][wW][sS][yY]'
    r'[mM][bB][oO][lL]\.[pP][yY]$"))',
    _DENY_W + r'[tT][eE][sS][tT][sS]/[mM][uU][tT][aA][tT][iI][oO][nN]_[cC][oO][nN][tT][rR]'
    r'[aA][cC][tT][dD][aA][tT][aA]\.[pP][yY]$"))',
    _DENY_W + r'[tT][eE][sS][tT][sS]/[mM][uU][tT][aA][tT][iI][oO][nN]_[iI][nN][fF][oO]'
    r'\.[pP][yY]$"))',
    _DENY_W + r'[tT][eE][sS][tT][sS]/[mM][uU][tT][aA][tT][iI][oO][nN]_[aA][lL][iI][aA][sS]'
    r'\.[pP][yY]$"))',
    _DENY_W + r'[tT][eE][sS][tT][sS]/[mM][uU][tT][aA][tT][iI][oO][nN]_[lL][iI][tT][eE][rR]'
    r'[aA][lL][sS]\.[pP][yY]$"))',
    _DENY_W + r'[tT][eE][sS][tT][sS]/[mM][uU][tT][aA][tT][iI][oO][nN]_[eE][nN][vV]'
    r'\.[pP][yY]$"))',
    _DENY_W + r'[dD][eE][pP][lL][oO][yY]\.[sS][hH]$"))',
    _DENY_W + r'[bB][iI][nN]/[cC][aA][pP][sS][uU][lL][eE]-[uU][nN][bB][lL][oO][cC][kK][eE]'
    r'[dD]-[hH][oO][oO][kK]\.[sS][hH]$"))',
    _DENY_W + r'[bB][iI][nN]/[bB][lL][iI][nN][dD]-[wW][aA][lL][lL]\.[pP][yY]$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[rR][uU][nN][nN][eE][rR][sS]/[sS][aA][nN][dD][bB]'
    r'[oO][xX]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[rR][uU][nN][nN][eE][rR][sS]/[sS][aA][nN][dD][bB]'
    r'[oO][xX](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[sS][lL][aA][cC][kK]_[lL][iI][sS][tT][eE][nN]'
    r'.*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[rR][uU][nN][nN][eE][rR][sS]/[dD][oO][cC][kK][eE]'
    r'[rR]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[rR][uU][nN][nN][eE][rR][sS]/[dD][oO][cC][kK][eE]'
    r'[rR](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[rR][uU][nN][nN][eE][rR][sS]/__[iI][nN][iI][tT]__'
    r'\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/__[iI][nN][iI][tT]__\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[jJ][oO][bB][sS]_[dD][iI][rR]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[jJ][oO][bB][sS]_[dD][iI][rR](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[dD][oO][cC][kK][eE][rR]_[dD][oO][oO][rR]'
    r'\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[dD][oO][cC][kK][eE][rR]_[dD][oO][oO][rR]'
    r'(/.*)?$"))',
    _DENY_W + r'[mM][aA][iI][nN][tT][eE][nN][aA][nN][cC][eE]/[eE][vV][eE][nN][tT][sS]'
    r'\..*$"))',
    _DENY_W + r'[mM][aA][iI][nN][tT][eE][nN][aA][nN][cC][eE]/[eE][vV][eE][nN][tT][sS]'
    r'(/.*)?$"))',
    _DENY_W + r'[mM][aA][iI][nN][tT][eE][nN][aA][nN][cC][eE]/[sS][cC][rR][uU][bB]\..*$"))',
    _DENY_W + r'[mM][aA][iI][nN][tT][eE][nN][aA][nN][cC][eE]/[sS][cC][rR][uU][bB](/.*)?$"))',
    _DENY_W + r'[mM][aA][iI][nN][tT][eE][nN][aA][nN][cC][eE]/[dD][oO][cC][kK][eE][rR]_'
    r'[eE][nN][gG][iI][nN][eE]\..*$"))',
    _DENY_W + r'[mM][aA][iI][nN][tT][eE][nN][aA][nN][cC][eE]/[dD][oO][cC][kK][eE][rR]_'
    r'[eE][nN][gG][iI][nN][eE](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[rR][uU][nN][nN][eE][rR][sS]/[hH][oO][sS][tT]_'
    r'[wW][aA][lL][lL]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[rR][uU][nN][nN][eE][rR][sS]/[hH][oO][sS][tT]_'
    r'[wW][aA][lL][lL](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[rR][uU][nN][nN][eE][rR][sS]/[mM][aA][cC]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[rR][uU][nN][nN][eE][rR][sS]/[mM][aA][cC](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[wW][oO][rR][kK][eE][rR]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[wW][oO][rR][kK][eE][rR](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[mM][oO][dD][eE][lL][sS]/[cC][lL][aA][uU][dD][eE]'
    r'\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[mM][oO][dD][eE][lL][sS]/[cC][lL][aA][uU][dD][eE]'
    r'(/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[mM][oO][dD][eE][lL][sS]/__[iI][nN][iI][tT]__'
    r'\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[sS][lL][aA][cC][kK]_[aA][nN][sS][wW][eE][rR]'
    r'\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[sS][lL][aA][cC][kK]_[aA][nN][sS][wW][eE][rR]'
    r'(/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[cC][oO][sS]_[aA][sS][kK]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[cC][oO][sS]_[aA][sS][kK](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[hH][uU][mM][aA][nN]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[hH][uU][mM][aA][nN](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[hH][uU][mM][aA][nN]_[pP][rR][eE][fF][lL][iI]'
    r'[gG][hH][tT]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[hH][uU][mM][aA][nN]_[pP][rR][eE][fF][lL][iI]'
    r'[gG][hH][tT](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[aA][nN][sS][wW][eE][rR]_[sS][cC][oO][pP][eE]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[aA][nN][sS][wW][eE][rR]_[sS][cC][oO][pP][eE]'
    r'(/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[sS][lL][aA][cC][kK]_[sS][eE][cC][rR][eE][tT]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[sS][lL][aA][cC][kK]_[sS][eE][cC][rR][eE][tT]'
    r'(/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[aA][nN][sS][wW][eE][rR]_[bB][aA][sS][eE]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[aA][nN][sS][wW][eE][rR]_[bB][aA][sS][eE]'
    r'(/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[rR][oO][wW]_[sS][cC][hH][eE][mM][aA][sS]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[rR][oO][wW]_[sS][cC][hH][eE][mM][aA][sS]'
    r'(/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[jJ][sS][oO][nN][sS][cC][hH][eE][mM][aA]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[jJ][sS][oO][nN][sS][cC][hH][eE][mM][aA]'
    r'(/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[bB][rR][aA][iI][nN][sS][tT][oO][rR][mM]_'
    r'[iI][nN][tT][aA][kK][eE]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[bB][rR][aA][iI][nN][sS][tT][oO][rR][mM]_'
    r'[iI][nN][tT][aA][kK][eE](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[bB][rR][aA][iI][nN][sS][tT][oO][rR][mM]_'
    r'[rR][uU][nN]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[bB][rR][aA][iI][nN][sS][tT][oO][rR][mM]_'
    r'[rR][uU][nN](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[bB][rR][aA][iI][nN][sS][tT][oO][rR][mM]_'
    r'[sS][tT][oO][rR][eE]\..*$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[bB][rR][aA][iI][nN][sS][tT][oO][rR][mM]_'
    r'[sS][tT][oO][rR][eE](/.*)?$"))',
    _DENY_W + r'[cC][aA][pP][sS][uU][lL][eE]/[pP][rR][oO][mM][pP][tT][sS]/'
    r'[bB][rR][aA][iI][nN][sS][tT][oO][rR][mM].*$"))',
    ";; rename guard: the directories above a protected path cannot be moved",
    _DENY_W + '[dD][oO][cC][sS]$"))',
    _DENY_W + '[cC][aA][pP][sS][uU][lL][eE]$"))',
    _DENY_W + '[dD][oO][cC][kK][eE][rR]$"))',
    _DENY_W + '[cC][aA][pP][sS][uU][lL][eE]/[bB][lL][iI][nN][dD]$"))',
    _DENY_W + '[mM][aA][iI][nN][tT][eE][nN][aA][nN][cC][eE]$"))',
    _DENY_W + '[tT][eE][sS][tT][sS]$"))',
    _DENY_W + '[bB][iI][nN]$"))',
    _DENY_W + '[cC][aA][pP][sS][uU][lL][eE]/[rR][uU][nN][nN][eE][rR][sS]$"))',
    _DENY_W + '[cC][aA][pP][sS][uU][lL][eE]/[mM][oO][dD][eE][lL][sS]$"))',
    _DENY_W + '[cC][aA][pP][sS][uU][lL][eE]/[pP][rR][oO][mM][pP][tT][sS]$"))',
    ";; env files: no read, no write (mv .env x would leak it)",
    r'(deny file-read* file-read-metadata file-write* (regex #"\.[eE][nN][vV]$")'
    r' (regex #"\.[eE][nN][vV]\.[^/]+$"))',
    ";; secrets and media: no read, no stat",
    '(deny file-read* file-read-metadata (subpath "/srv/example/.config/capsule")'
    ' (subpath "/srv/example/Movies"))',
    "",
])


class SeatbeltRender(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory(suffix=".a.b")   # a dot in the path must be escaped
        self.addCleanup(td.cleanup)
        self.root = os.path.realpath(td.name)
        self.wt = os.path.join(self.root, "wt")
        self.wt_re = re.sub(r"([.\[\](){}\\*+?|^$])", r"\\\1", self.wt)
        os.mkdir(self.wt)

    def test_golden_profile(self):
        got = seatbelt.render(self.wt, vault_read=[os.path.join(self.root, "vault")],
                              endpoints=["localhost:8080"], home=HOME)
        want = (GOLDEN.replace("{WTRE}", self.wt_re).replace("{WT}", self.wt)
                .replace("{VAULT}", os.path.join(self.root, "vault")))
        self.assertEqual(got, want)

    def test_write_outside_worktree_denied(self):
        text = seatbelt.render(self.wt, home=HOME)
        self.assertIn("(deny default)", text)
        writes = [ln for ln in text.splitlines()
                  if ln.startswith("(allow") and "file-write*" in ln]
        self.assertTrue(all(self.wt in ln or "/dev/" in ln for ln in writes), writes)
        self.assertNotIn("(allow file-write*)", text)

    def test_worktree_path_is_resolved(self):
        link = os.path.join(self.root, "link")
        os.symlink(self.wt, link)
        self.assertEqual(seatbelt.render(link, home=HOME), seatbelt.render(self.wt, home=HOME))

    def test_protected_path_write_denied_in_light_tier(self):
        text = seatbelt.render(self.wt, home=HOME)
        lines = text.splitlines()
        denies = [ln for ln in lines if ln.startswith("(deny file-write*")]
        for glob, ln in zip(workspace.PROTECTED, denies):   # one deny per glob, in order
            self.assertIn(self.wt_re, ln, glob)
        self.assertGreater(len(denies), len(workspace.PROTECTED))   # plus the rename guards
        # every deny comes after every allow, so it wins
        last_allow = max(i for i, ln in enumerate(lines) if ln.startswith("(allow"))
        first_deny = min(i for i, ln in enumerate(lines) if ln.startswith("(deny file"))
        self.assertLess(last_allow, first_deny)

    @staticmethod
    def _sweep():
        """Paths generated from PROTECTED: (must be denied, must not be denied)."""
        deny, allow = set(), set()
        for g in workspace.PROTECTED:
            fixed = not g.startswith("**")
            bare = g[3:] if g.startswith("**/") else g
            tails = ("", "/x", "/x/y.txt") if bare.endswith("/**") else ("",)
            for tail in tails:
                hit = bare.removesuffix("/**").replace("*", "zz") + tail
                for pre in ("",) if fixed else ("", "a/", "a/b/"):
                    deny.add(pre + hit)
            if fixed:   # every proper ancestor directory of the fixed prefix is guarded too
                parts = bare.removesuffix("/**").split("/")
                deny.update("/".join(parts[:i]) for i in range(1, len(parts)))
                allow.add(parts[0] + "2/" + "/".join(parts[1:]))   # prefix look-alike
            if "*" not in bare:
                allow.add(bare + "x")                              # suffix look-alike
        deny |= {p.upper() for p in deny} | {p.swapcase() for p in deny}   # case variants
        return deny, allow - deny

    def test_protected_sweep_from_protected_globs(self):
        text = seatbelt.render(self.wt, home=HOME)
        # the profile's ERE subset (classes, groups, .*, anchors) is also valid Python re
        regexes = [re.compile(m)
                   for m in re.findall(r'\(deny file-write\* \(regex #"([^"]*)"\)\)', text)]

        def denied(p):
            return any(r.match(self.wt + "/" + p) for r in regexes)
        deny, allow = self._sweep()
        self.assertGreater(len(deny), 60)
        for p in sorted(deny):
            self.assertTrue(denied(p), p)
        for p in sorted(allow):
            self.assertFalse(denied(p), p)
        # the profile may deny more than touches_protected, never less
        for p in sorted(deny):
            if workspace.touches_protected([p]):
                self.assertTrue(denied(p), p)
        for p in ("docs/RUNBOOK.md", "capsule/a.py", "README.md", "maintenance/x.py"):
            self.assertFalse(denied(p), p)

    def test_default_denies_secrets_and_media(self):
        text = seatbelt.render(self.wt, home=HOME)
        deny = [ln for ln in text.splitlines() if ln.startswith("(deny file-read*")]
        self.assertEqual(len(deny), 2)
        env, home = deny
        # stat is denied too (a global metadata allow would otherwise win); env files also
        # lose write, so `mv .env x` cannot move one to a readable name
        self.assertTrue(home.startswith("(deny file-read* file-read-metadata (subpath"), home)
        self.assertIn('(subpath "/srv/example/.config/capsule")', home)
        self.assertIn('(subpath "/srv/example/Movies")', home)
        self.assertTrue(env.startswith("(deny file-read* file-read-metadata file-write* (regex"))
        self.assertIn(r'(regex #"\.[eE][nN][vV]$")', env)
        self.assertIn(r'(regex #"\.[eE][nN][vV]\.[^/]+$")', env)   # a.env.local: no leading /
        self.assertGreater(text.index("(deny file-read*"), text.rindex("(allow"))

    def test_default_home_is_current_user(self):
        text = seatbelt.render(self.wt)
        self.assertIn(os.path.realpath(Path.home() / ".config" / "capsule"), text)
        self.assertIn(os.path.realpath(Path.home() / "Movies"), text)

    def test_extra_deny_read(self):
        secret = os.path.join(self.root, "secret")
        text = seatbelt.render(self.wt, home=HOME, deny_read=[secret])
        self.assertIn(f'(subpath "{secret}")', text)

    def test_network_limited_to_model_endpoint(self):
        self.assertNotIn("network-outbound", seatbelt.render(self.wt, home=HOME))
        text = seatbelt.render(self.wt, endpoints=["localhost:8080", "localhost:9090"], home=HOME)
        allows = [ln for ln in text.splitlines() if "network-outbound" in ln]
        self.assertEqual(allows, ['(allow network-outbound (remote tcp "localhost:8080"))',
                                  '(allow network-outbound (remote tcp "localhost:9090"))'])
        # row replay: only the localhost proxy port is allowed; names and IPs are refused (the
        # model host allow-list itself is the proxy's, card 22)
        proxy = seatbelt.render(self.wt, endpoints=["localhost:3128"], home=HOME)
        self.assertEqual(proxy.count("network-outbound"), 1)
        self.assertIn('(remote tcp "localhost:3128")', proxy)
        for bad in ("api.anthropic.com:443", "127.0.0.1:80", "localhost", "localhost:0",
                    "localhost:99999", "*:443;(allow network*)", "*", "*:443", "*:*",
                    "localhost:*"):
            with self.assertRaises(ValueError, msg=bad):
                seatbelt.render(self.wt, endpoints=[bad], home=HOME)

    def test_scratch_dir_is_inside_worktree(self):
        # no rule of its own: the worktree allow covers it; only a valid relative dir is accepted
        text = seatbelt.render(self.wt, scratch="tmp/x", home=HOME)
        self.assertIn(f'(subpath "{self.wt}")', text)
        self.assertNotIn("tmp/x", text)
        for bad in ("/tmp", "../out", "a/../../out", "", "."):
            with self.assertRaises(ValueError, msg=bad):
                seatbelt.render(self.wt, scratch=bad, home=HOME)

    def test_unsafe_path_characters_refused(self):
        for bad in ('wt"x', "wt\nx"):
            with self.assertRaises(ValueError):
                seatbelt.render(os.path.join(self.root, bad), home=HOME)


class _Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


@unittest.skipUnless(sys.platform == "darwin",
                     "light tier is unavailable off macOS (no sandbox-exec)")
class SeatbeltLive(unittest.TestCase):
    """Real sandbox-exec, temp dirs only. A broken sandbox-exec is a failure, not a skip (Z4)."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = os.path.realpath(td.name)
        self.wt = os.path.join(self.root, "wt")
        self.home = os.path.join(self.root, "home")
        for d in ("wt/contracts", "wt/docs", "wt/.worker-exec", "out", "vault",
                  "home/.config/capsule", "home/Movies"):
            os.makedirs(os.path.join(self.root, d))
        for f in ("wt/.env", "wt/prod.env", "vault/note.md", "out/o.txt",
                  "home/.config/capsule/k", "home/Movies/m.mov"):
            Path(self.root, f).write_text("data")
        Path(self.wt, "docs", "ZENKAI.md").write_text("z")
        self.profile = seatbelt.render(
            self.wt, vault_read=[os.path.join(self.root, "vault")], home=self.home)

    def run_sb(self, script, profile=None):
        return seatbelt.probe(profile or self.profile, script)

    def test_control_probe_runs(self):
        r = self.run_sb(f"echo hi > {self.wt}/ok.txt; cat {self.wt}/ok.txt")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "hi"), r.stderr)

    def test_write_outside_worktree_denied_live(self):
        self.assertEqual(self.run_sb(f"touch {self.wt}/in.txt").returncode, 0)
        self.assertTrue(os.path.exists(f"{self.wt}/in.txt"))
        for target in (f"{self.root}/out/new.txt", f"{self.root}/out/o.txt",
                       f"{self.root}/new.txt"):
            self.assertNotEqual(self.run_sb(f"touch {target}").returncode, 0, target)
        self.assertFalse(os.path.exists(f"{self.root}/new.txt"))
        self.assertFalse(os.path.exists(f"{self.root}/out/new.txt"))

    def test_write_outside_worktree_denied_row_cases_live(self):
        """Intent row write-outside-worktree-denied, with a temp worktree for /w/card."""
        os.makedirs(f"{self.wt}/src")
        os.makedirs(f"{self.root}/wt-sibling")      # shares the worktree's name as a prefix
        os.symlink(f"{self.root}/out", f"{self.wt}/out")   # a symlink out of the tree
        self.assertEqual(self.run_sb(f"touch {self.wt}/src/a.py").returncode, 0)
        for target in (f"{self.root}/wt-sibling/x.txt", f"{self.wt}/../escape.txt",
                       f"{self.wt}/out/x.txt"):
            self.assertNotEqual(self.run_sb(f"touch {target}").returncode, 0, target)
        for gone in ("wt-sibling/x.txt", "escape.txt", "out/x.txt"):
            self.assertFalse(os.path.exists(f"{self.root}/{gone}"), gone)

    def test_protected_path_write_denied_row_cases_live(self):
        """Intent row protected-path-write-denied-in-light-tier, with a temp worktree."""
        for d in ("agents/dev", "capsule", "contracts/intent"):
            os.makedirs(f"{self.wt}/{d}", exist_ok=True)
        denied = ("contracts/x.md", "contracts/intent/a.jsonl", "agents/SOUL.md",
                  "agents/dev/SOUL-dev.md", "CLAUDE.md", "docs/CLAUDE.md", "AGENTS.md",
                  "docs/ZENKAI.md", "capsule/intent.py", "capsule/workspace.py")
        for p in denied:
            self.assertNotEqual(self.run_sb(f"touch {self.wt}/{p}").returncode, 0, p)
        self.assertEqual(Path(self.wt, "docs", "ZENKAI.md").read_text(), "z")
        for p in denied:
            if p != "docs/ZENKAI.md":
                self.assertFalse(os.path.exists(f"{self.wt}/{p}"), p)
        self.assertEqual(self.run_sb(f"touch {self.wt}/capsule/a.py").returncode, 0)

    def test_protected_path_write_denied_in_light_tier_live(self):
        for cmd in (f"touch {self.wt}/contracts/x", f"touch {self.wt}/Contracts/y",
                    f"echo x > {self.wt}/docs/ZENKAI.md", f"touch {self.wt}/CLAUDE.md",
                    f"mv {self.wt}/contracts {self.wt}/moved"):
            self.assertNotEqual(self.run_sb(cmd).returncode, 0, cmd)
        self.assertEqual(os.listdir(f"{self.wt}/contracts"), [])
        self.assertEqual(Path(self.wt, "docs", "ZENKAI.md").read_text(), "z")
        self.assertFalse(os.path.exists(f"{self.wt}/CLAUDE.md"))
        # an unprotected sibling still works
        self.assertEqual(self.run_sb(f"touch {self.wt}/docs/other.md").returncode, 0)

    def test_rename_a_protected_parent_denied_live(self):
        """Swap a whole directory out from under a protected file: mv d d_old, plant a new d."""
        for d, f in (("capsule", "intent.py"), ("docs", "ZENKAI.md"), ("docker", "IMAGES.lock"),
                     ("capsule/blind", "x.py"), ("DOCS", "ZENKAI.md")):
            real = d.lower()
            os.makedirs(f"{self.wt}/{real}", exist_ok=True)
            Path(self.wt, real, f).write_text("orig")
            script = (f"mv {self.wt}/{d} {self.wt}/{real}_old && mkdir {self.wt}/cnew && "
                      f"echo EVIL > {self.wt}/cnew/{f} && mv {self.wt}/cnew {self.wt}/{d}")
            self.assertNotEqual(self.run_sb(script).returncode, 0, d)
            self.assertEqual(Path(self.wt, real, f).read_text(), "orig", d)
            self.assertFalse(os.path.exists(f"{self.wt}/{real}_old"), d)
            os.remove(f"{self.wt}/{real}/{f}")   # an empty dir: rmdir must still be refused
            self.assertNotEqual(self.run_sb(f"rmdir {self.wt}/{d}").returncode, 0, d)
            self.assertTrue(os.path.isdir(f"{self.wt}/{real}"), d)
            Path(self.wt, real, f).write_text("orig")
            # creating a child of the guarded directory still works
            self.assertEqual(self.run_sb(f"touch {self.wt}/{real}/new.txt").returncode, 0, d)

    def test_env_files_cannot_be_read_moved_or_statted_live(self):
        Path(self.wt, "a.env.local").write_text("data")
        for src in (".env", "prod.env", "a.env.local"):
            for cmd in (f"mv {self.wt}/{src} {self.wt}/plain2 && cat {self.wt}/plain2",
                        f"cat {self.wt}/{src}", f"/usr/bin/stat {self.wt}/{src}",
                        f"echo x > {self.wt}/{src}"):
                self.assertNotEqual(self.run_sb(cmd).returncode, 0, cmd)
            self.assertEqual(Path(self.wt, src).read_text(), "data", src)
        self.assertFalse(os.path.exists(f"{self.wt}/plain2"))
        # moving a plain file onto an env name is denied too
        Path(self.wt, "plain").write_text("p")
        self.assertNotEqual(self.run_sb(f"mv {self.wt}/plain {self.wt}/b.env").returncode, 0)

    def test_stat_of_secrets_denied_live(self):
        for p in ("home/.config/capsule/k", "home/Movies/m.mov"):
            r = self.run_sb(f"/usr/bin/stat {self.root}/{p}")
            self.assertNotEqual(r.returncode, 0, p)
            self.assertNotIn(str(len("data")), r.stdout, p)   # no size leak
        self.assertEqual(self.run_sb(f"/usr/bin/stat {self.wt}/docs/ZENKAI.md").returncode, 0)

    def test_scratch_dir_is_inside_worktree_live(self):
        self.assertEqual(self.run_sb(f"touch {self.wt}/.worker-exec/t").returncode, 0)
        self.assertTrue(os.path.exists(f"{self.wt}/.worker-exec/t"))

    def test_default_read_denies_live(self):
        for p in ("home/.config/capsule/k", "home/Movies/m.mov", "wt/.env", "wt/prod.env"):
            r = self.run_sb(f"cat {self.root}/{p}")
            self.assertNotEqual(r.returncode, 0, p)
            self.assertNotIn("data", r.stdout, p)
        r = self.run_sb(f"cat {self.root}/vault/note.md")
        self.assertEqual((r.returncode, r.stdout), (0, "data"), r.stderr)
        # a path no rule lists is denied too
        self.assertNotEqual(self.run_sb(f"cat {self.root}/out/o.txt").returncode, 0)

    def test_network_limited_to_model_endpoint_live(self):
        srv = http.server.HTTPServer(("127.0.0.1", 0), _Quiet)
        other = http.server.HTTPServer(("127.0.0.1", 0), _Quiet)
        for s in (srv, other):
            threading.Thread(target=s.serve_forever, daemon=True).start()
            self.addCleanup(s.server_close)
            self.addCleanup(s.shutdown)
        curl = "/usr/bin/curl -s -m 5 http://127.0.0.1:%d/"
        allow = seatbelt.render(
            self.wt, endpoints=[f"localhost:{srv.server_port}"], home=self.home)
        r = self.run_sb(curl % srv.server_port, allow)
        self.assertEqual((r.returncode, r.stdout), (0, "ok"), r.stderr)
        # another port, no endpoints at all, and a non-allowlisted external host all fail
        self.assertNotEqual(self.run_sb(curl % other.server_port, allow).returncode, 0)
        self.assertNotEqual(self.run_sb(curl % srv.server_port).returncode, 0)
        external = "/usr/bin/curl -s -m 5 http://93.184.216.34/"
        self.assertNotEqual(self.run_sb(external, allow).returncode, 0)


if __name__ == "__main__":
    unittest.main()
