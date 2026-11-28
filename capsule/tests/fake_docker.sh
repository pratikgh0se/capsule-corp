#!/bin/bash
# Tests only: a `docker` CLI over the host shell, for tests that spawn the real landing worker (a
# child process, where the in-process host stand-in of host_box.py does not reach). `run` remembers
# the mount at /workspace, `exec` runs the command in that directory with /workspace and /trusted
# mapped to the clone and the trusted checkout, `inspect` answers the mount audit, `rm` and the
# rest do nothing. State dir: $FAKE_DOCKER_STATE. Plain bash, not python: the lander calls it
# about ten times per landing and a python start costs ~40 ms of cpu against ~5 ms here.
args=("$@")
after() {   # after <flag> <offset>: the argument <offset> places after the first <flag>
    for i in "${!args[@]}"; do
        [ "${args[$i]}" = "$1" ] && { printf '%s' "${args[$(($i + $2))]}"; return; }
    done
}
case "${args[0]}" in
run)
    for a in "${args[@]}"; do
        case "$a" in
            *:/workspace) printf '%s' "${a%%:*}" > "$FAKE_DOCKER_STATE/$(after --name 1)"; break;;
        esac
    done;;
exec)
    src=$(cat "$FAKE_DOCKER_STATE/$(after -w 2)" 2>/dev/null)
    [ -n "$src" ] && cd "$src" || exit 1   # unknown container or missing clone
    cmd=${args[$((${#args[@]} - 1))]}
    keep=/workspace/.worker-exec   # the image's TMPDIR is not in the clone: leave it alone
    cmd=${cmd//"$keep"/@@keep@@}
    cmd=${cmd//\/workspace/"$src"}
    cmd=${cmd//@@keep@@/"$keep"}
    AGENT_OPS_NO_SEND=1 exec /bin/sh -c "${cmd//\/trusted/"$FAKE_DOCKER_TRUSTED"}";;
inspect)
    case " ${args[*]} " in
        *" -f "*) echo true;;
        *) echo '[{"Mounts": [{"Source": "/w"}]}]';;
    esac;;
esac
exit 0
