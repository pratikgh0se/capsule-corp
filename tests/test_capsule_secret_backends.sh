#!/usr/bin/env bash
# bin/capsule-secret backends (P06): file (0600 only), env, and the infisical
# path staying selectable. Temp HOME and CAPSULE_HOME, fake values only; the real
# infisical CLI and the real config dir are never touched.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

fails=0
PASSED=""
check() { if eval "$2"; then echo "PASS $1"; PASSED="$PASSED|$1"; else echo "FAIL $1"; fails=$((fails+1)); fi; }

export HOME="$T/home"; mkdir -p "$HOME"
export CAPSULE_HOME="$T/capsule"; mkdir -p "$CAPSULE_HOME"
export CAPSULE_CONFIG_DIR="$T/config"        # empty: no infisical layout
unset CAPSULE_SECRETS_BACKEND CAPSULE_IDENTITY INFISICAL_BIN
CS="$REPO/bin/capsule-secret"
SF="$CAPSULE_HOME/secrets.env"

printf '# comment\nFAKE_ONE=fake-value-one\nexport FAKE_TWO="fake two quoted"\nFAKE_THREE='"'"'fake-three'"'"'\n\nFAKE_ONE=fake-value-one-later\n' >"$SF"

# --- file backend, the default ---
chmod 644 "$SF"
out="$("$CS" get demo FAKE_ONE 2>&1)"; rc=$?
check "file-mode-0644-refused: exit nonzero" '[ $rc -eq 3 ]'
check "file-mode-0644-refused: names the mode, leaks no value" 'grep -q "mode is 644" <<<"$out" && ! grep -q "fake-value" <<<"$out"'
check "file-mode-0644-refused: run does not start the command" \
  '! "$CS" run demo -- touch "$T/ran" >/dev/null 2>&1 && [ ! -e "$T/ran" ]'
check "file-mode-0644-refused: check fails too" '! "$CS" check >/dev/null 2>&1'
chmod 640 "$SF"; check "mode 0640 refused" '! "$CS" get demo FAKE_ONE >/dev/null 2>&1'
chmod 700 "$SF"; check "mode 0700 refused" '! "$CS" get demo FAKE_ONE >/dev/null 2>&1'

chmod 600 "$SF"
check "default backend is file: get returns the file's last value for the key" \
  '[ "$("$CS" get demo FAKE_ONE)" = "$(grep "^FAKE_ONE=" "$SF" | tail -n1 | cut -d= -f2-)" ]'
check "double-quoted value is unquoted" '[ "$("$CS" get demo FAKE_TWO)" = "fake two quoted" ]'
check "single-quoted value is unquoted" '[ "$("$CS" get demo FAKE_THREE)" = "fake-three" ]'

out="$("$CS" get demo FAKE_MISSING 2>&1)"; rc=$?
check "missing-key-is-nonzero-exit (file)" '[ $rc -eq 3 ] && grep -q "no such secret: FAKE_MISSING" <<<"$out"'
check "get with a bad name is refused" '! "$CS" get demo "A B" >/dev/null 2>&1'
rm -f "$T/nofile"; out="$(CAPSULE_HOME="$T/empty-home" "$CS" get demo FAKE_ONE 2>&1)"; rc=$?
check "absent secrets file is nonzero and names the path" '[ $rc -eq 3 ] && grep -qF "$T/empty-home/secrets.env" <<<"$out"'

out="$("$CS" run demo -- sh -c 'printf "%s|%s" "$FAKE_TWO" "$FAKE_THREE"')"
check "run injects the file's keys into the command" '[ "$out" = "fake two quoted|fake-three" ]'
out="$("$CS" export demo)"
check "export prints dotenv lines" 'grep -qx "FAKE_THREE='"'"'fake-three'"'"'" <<<"$out"'
check "check passes on a 0600 file" '[ "$("$CS" check)" = "capsule-secret: ok (file)" ]'
check "check --identity-all passes on a 0600 file" '"$CS" check --identity-all >/dev/null'
check "the file is not executed (a command line in it does nothing)" \
  'printf "FAKE_X=1\n\$(touch %s)\n" "$T/pwned" >"$SF"; chmod 600 "$SF"; "$CS" get demo FAKE_X >/dev/null; [ ! -e "$T/pwned" ]'

# --- env backend ---
export CAPSULE_SECRETS_BACKEND=env
chmod 644 "$SF"   # the file is irrelevant to the env backend
check "env get returns the process variable" '[ "$(FAKE_ENV_KEY=fake-env-value "$CS" get demo FAKE_ENV_KEY)" = "fake-env-value" ]'
out="$("$CS" get demo FAKE_ENV_UNSET 2>&1)"; rc=$?
check "missing-key-is-nonzero-exit (env)" '[ $rc -eq 3 ] && grep -q "no such secret: FAKE_ENV_UNSET" <<<"$out"'
check "env run passes the environment through" '[ "$(FAKE_ENV_KEY=fake-env-value "$CS" run demo -- sh -c "printf %s \"\$FAKE_ENV_KEY\"")" = "fake-env-value" ]'
check "env check is ok" '[ "$("$CS" check)" = "capsule-secret: ok (env)" ]'
check "env export is refused" '! "$CS" export demo >/dev/null 2>&1'

# --- unknown backend ---
export CAPSULE_SECRETS_BACKEND=bogus
out="$("$CS" check 2>&1)"; rc=$?
check "unknown backend exits 3" \
  '[ $rc -eq 3 ] && grep -q "unknown backend" <<<"$out" && ! grep -q bogus <<<"$out"'
for v in File " file "; do
  check "backend name must match exactly: '$v' exits 3" \
    'out="$(CAPSULE_SECRETS_BACKEND="$v" "$CS" backend 2>&1)"; rc=$?
     [ $rc -eq 3 ] && grep -q "unknown backend" <<<"$out"'
done

# --- infisical: explicit selection and the legacy auto-detect ---
# A fake infisical records that it was called; identity and project files are fake.
mkdir -p "$T/fb" "$T/inf/identities"
cat >"$T/fb/infisical" <<'EOF'
#!/usr/bin/env bash
case "$1" in login) echo faketok ;; *) echo "infisical-value" ;; esac
EOF
chmod +x "$T/fb/infisical"
echo "PROJECT_DEMO=proj-fake" >"$T/inf/projects.env"
printf 'INFISICAL_CLIENT_ID=fake-id\nINFISICAL_CLIENT_SECRET=fake-secret\n' >"$T/inf/identities/bridge.env"
inf() { CAPSULE_CONFIG_DIR="$T/inf" CAPSULE_IDENTITY=bridge CAPSULE_SECRET_WAIT_S=0 INFISICAL_BIN="$T/fb/infisical" "$CS" "$@"; }

export CAPSULE_SECRETS_BACKEND=infisical
check "backend-infisical-unchanged: explicit infisical resolves through the CLI" '[ "$(inf get demo FAKE_ONE)" = "infisical-value" ]'
check "backend-infisical-unchanged: explicit infisical still needs an identity" \
  '! env -u CAPSULE_IDENTITY CAPSULE_CONFIG_DIR="$T/inf" CAPSULE_SECRET_WAIT_S=0 INFISICAL_BIN="$T/fb/infisical" "$CS" get demo FAKE_ONE >/dev/null 2>&1'
unset CAPSULE_SECRETS_BACKEND
rm -f "$SF"
check "backend-infisical-unchanged: unset backend + existing infisical config + no secrets.env stays infisical" \
  '[ "$(inf get demo FAKE_ONE)" = "infisical-value" ]'
printf 'FAKE_ONE=from-file\n' >"$SF"; chmod 600 "$SF"
out="$(inf get demo FAKE_ONE 2>&1)"; rc=$?
check "both secrets.env and an infisical config, backend unset: ambiguous, exit 3" \
  '[ $rc -eq 3 ] && grep -q "ambiguous: set CAPSULE_SECRETS_BACKEND" <<<"$out"'
check "ambiguity is resolved by naming the backend" '[ "$(CAPSULE_SECRETS_BACKEND=file inf get demo FAKE_ONE)" = "from-file" ]'

# --- host.json secrets.backend (SEC-14): env var, then host file, then autodetect ---
HH="$T/hosthome"; mkdir -p "$HH" "$T/hcfg"
touch "$HH/secrets.env" "$T/hcfg/projects.env"
hb() {
  CAPSULE_HOME="$HH" CAPSULE_CONFIG_DIR="$T/hcfg" AGENT_OPS_NO_SEND=1 "$CS" backend "$@"
}
printf '{"secrets": {"backend": "env"}}' >"$HH/host.json"
check "host-1: host.json backend is used, whatever files exist" '[ "$(hb)" = env ] && hb >/dev/null'
printf '{"secrets.backend": "infisical"}' >"$HH/host.json"
check "host-1: flat dotted key works too" '[ "$(hb)" = infisical ]'
printf '{"secrets": {"backend": "env"}}' >"$HH/host.json"
check "host-2: the env var beats host.json" '[ "$(CAPSULE_SECRETS_BACKEND=file hb)" = file ]'
rm -f "$HH/host.json"
out="$(hb 2>&1)"; rc=$?
check "host-3: no host file, both files: ambiguous, names the env var" \
  '[ $rc -ne 0 ] && grep -q "CAPSULE_SECRETS_BACKEND" <<<"$out"'
rm -f "$HH/secrets.env"; check "host-3: projects.env alone is infisical" '[ "$(hb)" = infisical ]'
rm -f "$T/hcfg/projects.env"; check "host-3: neither file is file" '[ "$(hb)" = file ]'
printf '{"secrets": {"backend": "bogus-fake"}}' >"$HH/host.json"
out="$(hb 2>&1)"; rc=$?
check "host-4: unknown backend refused, names host.json and secrets.backend" \
  '[ $rc -ne 0 ] && grep -q "host.json" <<<"$out" && grep -q "secrets.backend" <<<"$out"'
printf '{not json' >"$HH/host.json"
out="$(hb 2>&1)"; rc=$?
check "host-4: bad JSON refused, names host.json and secrets.backend" \
  '[ $rc -ne 0 ] && grep -q "host.json" <<<"$out" && grep -q "secrets.backend" <<<"$out"'
printf '{"secrets": {}}' >"$HH/host.json"
check "host-5: a host file without the key falls through to autodetect" '[ "$(hb)" = file ]'
touch "$T/hcfg/projects.env"
check "host-5: and to infisical when projects.env exists" '[ "$(hb)" = infisical ]'
printf '{"secrets": {"backend": "bogus-fake-secret-value"}}' >"$HH/host.json"
out="$(hb 2>&1)"
check "host-6: a refusal prints no value from the file" \
  '! grep -q "bogus-fake-secret-value" <<<"$out"'

# --- host.json reader: interpreter gate, launcher stderr, dispatch message (fakes only) ---
printf '{"secrets": {"backend": "env"}}' >"$HH/host.json"
mkdir -p "$T/fakepy"
printf '#!/bin/sh\nfor a in "$@"; do [ "$a" = -c ] && { echo 3.9.18; exit 0; }; done\necho "Unknown option: -P" >&2; exit 2\n' >"$T/fakepy/py39"
chmod +x "$T/fakepy/py39"
out="$(CAPSULE_PY="$T/fakepy/py39" hb 2>&1)"; rc=$?
check "py-1: Python 3.9 gets a clear message, nonzero rc, no raw -P error" \
  '[ $rc -eq 3 ] && grep -qF "capsule-secret: needs Python 3.11+ (CAPSULE_PY=$T/fakepy/py39 is 3.9.18)" <<<"$out" && ! grep -q "Unknown option" <<<"$out"'
printf '#!/bin/sh\nexit 1\n' >"$T/fakepy/pybad"; chmod +x "$T/fakepy/pybad"
out="$(CAPSULE_PY="$T/fakepy/pybad" hb 2>&1)"; rc=$?
check "py-1: an interpreter that cannot report a version is refused the same way" \
  '[ $rc -ne 0 ] && grep -q "needs Python 3.11+" <<<"$out" && grep -q "is unknown" <<<"$out"'
printf '#!/bin/sh\necho "launcher-noise-fake" >&2\necho env\n' >"$T/fakepy/launcher"; chmod +x "$T/fakepy/launcher"
out="$(CAPSULE_BIN="$T/fakepy/launcher" hb 2>&1)"; rc=$?
check "py-2: launcher stderr on success is not reported as an error" \
  '[ $rc -eq 0 ] && [ "$out" = env ] && ! grep -q "unknown backend" <<<"$out"'
check "py-3: the default-deny dispatch arm stays and does not echo the value" \
  'grep -qF "*) die \"unknown CAPSULE_SECRETS_BACKEND (valid: file, infisical, env)\" ;;" "$CS" && ! grep -q "unknown CAPSULE_SECRETS_BACKEND: " "$CS"'
printf '#!/bin/sh\nfor a in "$@"; do [ "$a" = -c ] && { echo 3.12.1; exit 0; }; done\necho env\n' >"$T/fakepy/py312"
chmod +x "$T/fakepy/py312"
out="$(CAPSULE_PY="$T/fakepy/py312" hb 2>&1)"; rc=$?
check "py-4: a 3.11+ CAPSULE_PY passes the gate and reads the backend" '[ $rc -eq 0 ] && [ "$out" = env ]'
mkdir -p "$T/sitecustom"
printf 'print("SITECUSTOMIZE-MARKER-FAKE")\n' >"$T/sitecustom/sitecustomize.py"
out="$(PYTHONPATH="$T/sitecustom" hb 2>&1)"; rc=$?
check "py-5: sitecustomize output on PYTHONPATH does not leak and a valid version passes" \
  '[ $rc -eq 0 ] && ! grep -q "SITECUSTOMIZE-MARKER-FAKE" <<<"$out" && ! grep -q "needs Python" <<<"$out"'
rm -f "$HH/host.json"

# --- file guard: symlink, owner; parser warnings; empty values ---
export CAPSULE_SECRETS_BACKEND=file
cp "$SF" "$T/real.env"; chmod 600 "$T/real.env"; rm -f "$SF"; ln -s "$T/real.env" "$SF"
out="$("$CS" get demo FAKE_ONE 2>&1)"; rc=$?
check "file-symlink-refused: a symlinked secrets.env exits nonzero naming the symlink" \
  '[ $rc -ne 0 ] && grep -q "symlink" <<<"$out" && ! grep -q "from-file" <<<"$out"'
rm -f "$SF"; cp "$T/real.env" "$SF"; chmod 600 "$SF"
# Owner check: shadow `id` so the current user looks like someone else (cannot chown without root).
mkdir -p "$T/idbin"; printf '#!/bin/sh\necho 99999\n' >"$T/idbin/id"; chmod +x "$T/idbin/id"
out="$(PATH="$T/idbin:$PATH" "$CS" get demo FAKE_ONE 2>&1)"; rc=$?
check "file-wrong-owner-refused: owner uid differing from the user is refused" \
  '[ $rc -ne 0 ] && grep -q "owned by uid" <<<"$out" && ! grep -q "from-file" <<<"$out"'
check "file owned by the current user is accepted" '[ "$("$CS" get demo FAKE_ONE)" = "from-file" ]'
printf 'GOOD_ONE=1\nthis line has no equals sign SECRETWORD\n9bad=2\nEMPTY_ONE=\n' >"$SF"
out="$("$CS" get demo GOOD_ONE 2>&1)"
check "unparseable lines warn with the line number only, never the content" \
  'grep -q "line 2 ignored" <<<"$out" && grep -q "line 3 ignored" <<<"$out" && ! grep -q "SECRETWORD" <<<"$out" && grep -q "^1$" <<<"$out"'
"$CS" get demo EMPTY_ONE >/dev/null 2>&1; erc=$?
check "file backend: an empty value is present, exit 0" '[ $erc -eq 0 ]'
export CAPSULE_SECRETS_BACKEND=env
EMPTY_E= "$CS" get demo EMPTY_E >/dev/null 2>&1; erc=$?
check "env backend: an empty value is present, exit 0" '[ $erc -eq 0 ]'
check "env backend: a script default (CURL_BIN) is not offered as a secret" \
  'env -u CURL_BIN -u INFISICAL_BIN "$CS" get demo CURL_BIN >/dev/null 2>&1; [ $? -eq 3 ]'
unset CAPSULE_SECRETS_BACKEND

# --- compose profile follows the backend (lib_compose.sh) ---
prof() { env "$@" COMPOSE_FILE=x bash -c '. "$0/maintenance/lib_compose.sh"; echo "[$COMPOSE_PROFILES][${CC_CADDY_SECRETS:-}]"' "$REPO"; }
check "file backend: no compose profile, no secrets route" '[ "$(prof CAPSULE_SECRETS_BACKEND=file)" = "[][]" ]'
check "env backend: no compose profile" '[ "$(prof CAPSULE_SECRETS_BACKEND=env)" = "[][]" ]'
check "infisical backend: profile secrets and the secrets route" \
  '[ "$(prof CAPSULE_SECRETS_BACKEND=infisical)" = "[secrets][/etc/caddy/routes/secrets.caddy]" ]'
check "an explicit COMPOSE_PROFILES wins" '[ "$(prof CAPSULE_SECRETS_BACKEND=file COMPOSE_PROFILES=secrets)" = "[secrets][/etc/caddy/routes/secrets.caddy]" ]'
check "backend subcommand failing makes lib_compose keep the secrets profile on (never off)" \
  '[ "$(prof CAPSULE_HOME="$T/capsule" CAPSULE_CONFIG_DIR="$T/inf" 2>/dev/null)" = "[secrets][/etc/caddy/routes/secrets.caddy]" ]'
check "that failure prints one stderr line" \
  '[ "$(env CAPSULE_HOME="$T/capsule" CAPSULE_CONFIG_DIR="$T/inf" COMPOSE_FILE=x bash -c ". \"$REPO/maintenance/lib_compose.sh\"" 2>&1 >/dev/null | grep -c "backend failed")" = 1 ]'
check "compose_up_all skips the infisical phases without the secrets profile" \
  '[ "$(env CAPSULE_SECRETS_BACKEND=file COMPOSE_FILE=x bash -c ". \"$REPO/maintenance/lib_compose.sh\"; compose_up_infisical() { echo INF; }; compose_wait_infisical() { echo WAIT; }; compose_up_rest() { echo REST; }; compose_up_all")" = REST ]'
check "compose_up_all runs infisical, wait, then rest under the secrets profile" \
  '[ "$(env CAPSULE_SECRETS_BACKEND=infisical COMPOSE_FILE=x bash -c ". \"$REPO/maintenance/lib_compose.sh\"; compose_up_infisical() { echo INF; }; compose_wait_infisical() { echo WAIT; }; compose_up_rest() { echo REST; }; compose_up_all" | tr "\n" " ")" = "INF WAIT REST " ]'
check "backend subcommand prints the effective backend" '[ "$(CAPSULE_SECRETS_BACKEND=env "$CS" backend)" = env ]'

# --- host.json secrets.backend: env var > host.json > autodetect ---
export AGENT_OPS_NO_SEND=1
HJ="$CAPSULE_HOME/host.json"
hb() { printf '%s\n' "$1" >"$HJ"; shift; env "$@" "$CS" backend 2>&1; }
mkdir -p "$T/both"; : >"$T/both/projects.env"; printf 'A=1\n' >"$SF"
unset CAPSULE_SECRETS_BACKEND
for b in file infisical env; do
  check "host.json $b: ambiguity not raised, sets backend" \
    '[ "$(hb "{\"secrets\":{\"backend\":\"$b\"}}" CAPSULE_CONFIG_DIR="$T/both")" = "$b" ]'
done
check "env var wins over an invalid host.json" \
  '[ "$(hb "{bad" CAPSULE_SECRETS_BACKEND=env)" = env ]'
check "env var wins over host.json" \
  '[ "$(hb "{\"secrets\":{\"backend\":\"file\"}}" CAPSULE_SECRETS_BACKEND=env)" = env ]'
check "empty env var falls to host.json" \
  '[ "$(hb "{\"secrets\":{\"backend\":\"env\"}}" CAPSULE_SECRETS_BACKEND=)" = env ]'
for j in '{}' '{"secrets":{"backend":null}}'; do
  check "host.json without a backend ($j): autodetect, both files is ambiguous" \
    'out="$(hb "$j" CAPSULE_CONFIG_DIR="$T/both")"; rc=$?
     [ $rc -eq 3 ] && grep -q "ambiguous" <<<"$out"'
  check "host.json without a backend ($j): projects.env alone is infisical" \
    'rm -f "$SF"; [ "$(hb "$j" CAPSULE_CONFIG_DIR="$T/both")" = infisical ]'
  check "host.json without a backend ($j): neither file is file" \
    '[ "$(hb "$j" CAPSULE_CONFIG_DIR="$T/none")" = file ]'
  printf 'A=1\n' >"$SF"
done
for j in '{bad' '[1]' '{"secrets":{"backend":"zz-leak"}}'; do
  check "invalid host.json ($j): exit 3, names host.json, never the value" \
    'out="$(hb "$j"; echo "rc=$?")"; HJ_RC=$(env "$CS" get demo A >/dev/null 2>&1; echo $?);
     [ "$HJ_RC" = 3 ] && grep -q "host.json" <<<"$out" && ! grep -q "zz-leak" <<<"$out"'
done
check "invalid host.json refuses every subcommand with exit 3" \
  'printf "{bad\n" >"$HJ"; n=0
   for c in "get demo A" "export demo" check "run demo -- true" backend; do
     "$CS" $c >/dev/null 2>&1; [ $? -eq 3 ] && n=$((n+1)); done; [ $n -eq 5 ]'
check "host.json env: get reads the environment, not a same-named secrets.env entry" \
  'printf "{\"secrets\":{\"backend\":\"env\"}}\n" >"$HJ"; printf "NAME_X=fromfile\n" >"$SF"
   chmod 600 "$SF"; [ "$(NAME_X=fromenv "$CS" get demo NAME_X)" = fromenv ]'
rm -f "$HJ"; mkdir "$HJ"
check "host.json as a directory is refused, exit 3" \
  'out="$("$CS" backend 2>&1)"; rc=$?
   [ $rc -eq 3 ] && grep -q "refusing host.json secrets.backend:" <<<"$out"'
rmdir "$HJ"; ln -s "$T/nowhere.json" "$HJ"
check "host.json as a dangling link is refused, exit 3" \
  '"$CS" backend >/dev/null 2>&1; [ $? -eq 3 ]'
rm -f "$HJ"
# The reader is found from the script's real path (symlink), else the installed launcher.
printf '{"secrets":{"backend":"env"}}\n' >"$HJ"
mkdir -p "$T/lnk" "$T/cp"; ln -s "$CS" "$T/lnk/capsule-secret"; cp "$CS" "$T/cp/capsule-secret"
printf '#!/bin/sh\necho env\n' >"$T/cp/ok-capsule"; chmod +x "$T/cp/ok-capsule"
printf '#!/bin/sh\necho "capsule host: boom" >&2; exit 1\n' >"$T/cp/bad-capsule"
chmod +x "$T/cp/bad-capsule"
check "symlinked script run from / reads host.json" \
  '[ "$(cd / && "$T/lnk/capsule-secret" backend)" = env ]'
check "copied script uses the launcher (CAPSULE_BIN)" \
  '[ "$(cd / && CAPSULE_BIN="$T/cp/ok-capsule" "$T/cp/capsule-secret" backend)" = env ]'
check "copied script, failing launcher: one refusing line, exit 3, no doubled path" \
  'out="$(CAPSULE_BIN="$T/cp/bad-capsule" "$T/cp/capsule-secret" backend 2>&1)"; rc=$?
   [ $rc -eq 3 ] && [ "$out" = "capsule-secret: refusing host.json secrets.backend: boom" ]'
check "copied script, no launcher: exit 3, install it or set CAPSULE_BIN" \
  'out="$(CAPSULE_BIN= "$T/cp/capsule-secret" backend 2>&1)"; rc=$?
   [ $rc -eq 3 ] && grep -q "install it or set CAPSULE_BIN" <<<"$out"'
check "a launcher answer that is not a backend word is refused" \
  'printf "#!/bin/sh\necho zz-leak\n" >"$T/cp/odd-capsule"; chmod +x "$T/cp/odd-capsule"
   out="$(CAPSULE_BIN="$T/cp/odd-capsule" "$T/cp/capsule-secret" backend 2>&1)"; rc=$?
   [ $rc -eq 3 ] && ! grep -q "zz-leak" <<<"$out"'
# A 3.9-like python3 first on PATH must not matter when the launcher works.
mkdir -p "$T/fakepy"
printf '#!/bin/sh\necho "SyntaxError: old python" >&2\nexit 1\n' >"$T/fakepy/python3"
chmod +x "$T/fakepy/python3"
check "old python3 on PATH, working launcher: host.json still read" \
  '[ "$(cd / && PATH="$T/fakepy:$PATH" CAPSULE_BIN="$T/cp/ok-capsule" \
     "$T/cp/capsule-secret" backend)" = env ]'
# Ruling 2: only the file and secrets.backend matter; import-time noise stays off stdout.
for j in '{"nope":1,"secrets":{"backend":"env"}}' \
         '{"launchd":{"label_prefix":"bad prefix"},"vault":5,"secrets":{"backend":"env"}}'; do
  check "unrelated bad key does not refuse ($j)" \
    'printf "%s\n" "$j" >"$HJ"; [ "$(CAPSULE_BIN= "$CS" backend 2>&1)" = env ]'
done
check "unrelated bad key and no backend falls to autodetect" \
  'printf "{\"nope\":1}\n" >"$HJ"
   [ "$(CAPSULE_BIN= CAPSULE_CONFIG_DIR="$T/none" "$CS" backend)" = file ]'
check "PYTHONPROFILEIMPORTTIME does not pollute the answer" \
  'printf "{\"secrets\":{\"backend\":\"env\"}}\n" >"$HJ"
   [ "$(PYTHONPROFILEIMPORTTIME=1 CAPSULE_BIN= "$CS" backend 2>/dev/null)" = env ]'
check "PYTHONPROFILEIMPORTTIME: a refusal is still one clean line" \
  'printf "{\"secrets\":{\"backend\":\"zz\"}}\n" >"$HJ"
   out="$(PYTHONPROFILEIMPORTTIME=1 CAPSULE_BIN= "$CS" backend 2>&1)"; rc=$?
   [ $rc -eq 3 ] && [ "$(wc -l <<<"$out" | tr -d " ")" = 1 ]
   grep -q "refusing host.json" <<<"$out"'
# The reader keeps PYTHONPATH, so it cannot use -I: -s keeps a user-site .pth out of it.
check "a user-site .pth does not change the answer" \
  'printf "{\"secrets\":{\"backend\":\"env\"}}\n" >"$HJ"
   us="$(PYTHONUSERBASE="$T/ub" "${CAPSULE_PY:-python3}" -c "import site; print(site.getusersitepackages())")"
   mkdir -p "$us"
   printf "import sys; sys.stdout.write(\"POLLUTED\\n\")\n" >"$us/zz.pth"
   out="$(PYTHONUSERBASE="$T/ub" CAPSULE_BIN= "$CS" backend 2>&1 </dev/null)"
   [ "$out" = env ]'
printf '{"secrets":{"backend":"env"}}\n' >"$HJ"
check "copied script, env var set: host.json never consulted" \
  'out="$(CAPSULE_SECRETS_BACKEND=file CAPSULE_BIN="$T/none" "$T/cp/capsule-secret" backend)"
   [ "$out" = file ]'
# Round 2: FIFO, time limit, explicit CAPSULE_BIN, non-object section, temp file, reader errors.
lim() { perl -e 'alarm shift; exec @ARGV' 20 "$@"; }
mklauncher() { printf '#!/bin/sh\n%b\n' "$2" >"$T/cp/$1"; chmod +x "$T/cp/$1"; }
rm -f "$HJ"; mkfifo "$HJ"
check "host.json as a FIFO is refused without reading it, exit 3" \
  'out="$(CAPSULE_BIN= lim "$CS" backend 2>&1)"; rc=$?
   [ $rc -eq 3 ] && grep -q "refusing host.json secrets.backend: not a regular file" <<<"$out"'
rm -f "$HJ"; printf '{"secrets":{"backend":"env"}}\n' >"$HJ"
mklauncher sleep-capsule 'exec sleep 30'
check "a reader that outlives the time limit is refused, exit 3" \
  'out="$(CAPSULE_SECRET_READ_S=1 CAPSULE_BIN="$T/cp/sleep-capsule" "$CS" backend 2>&1)"; rc=$?
   [ $rc -eq 3 ] && grep -q "timed out" <<<"$out"'
check "an explicit CAPSULE_BIN that is not executable refuses, naming CAPSULE_BIN" \
  'out="$(CAPSULE_BIN="$T/none" "$CS" backend 2>&1)"; rc=$?
   [ $rc -eq 3 ] && grep -q "CAPSULE_BIN" <<<"$out"'
check "a non-executable default launcher is skipped silently (repo reader used)" \
  '[ "$(HOME="$T/home" CAPSULE_BIN= "$CS" backend)" = env ]'
printf '{"secrets":"infisical"}\n' >"$HJ"
check "a non-object secrets section refuses like the full reader" \
  'out="$(CAPSULE_BIN= "$CS" backend 2>&1)"; rc=$?
   [ $rc -eq 3 ] && grep -q "secrets must be an object" <<<"$out"'
printf '{"secrets":{"backend":"env"}}\n' >"$HJ"
mkdir -p "$T/tmpd"
check "the stderr temp file is removed after success and after a refusal" \
  'TMPDIR="$T/tmpd" CAPSULE_BIN= "$CS" backend >/dev/null
   printf "{bad\n" >"$HJ"; TMPDIR="$T/tmpd" CAPSULE_BIN= "$CS" backend >/dev/null 2>&1
   printf "{\"secrets\":{\"backend\":\"env\"}}\n" >"$HJ"; [ -z "$(ls "$T/tmpd")" ]'
mklauncher term-capsule 'p=$PPID; l=""
for _ in 1 2 3; do
  l="$l $p"; p="$(ps -o ppid= -p "$p" | tr -d " ")"; [ -n "$p" ] || break
done
for p in $l; do
  case "$(ps -o command= -p "$p")" in *capsule-secret*) kill -TERM "$p" ;; esac
done
sleep 1'
check "TERM during the read removes the temp file and exits 143" \
  'TMPDIR="$T/tmpd" CAPSULE_BIN="$T/cp/term-capsule" "$CS" backend >/dev/null 2>&1; rc=$?
   [ $rc -eq 143 ] && [ -z "$(ls "$T/tmpd")" ]'
mklauncher noisy-capsule 'echo "odd launcher failure" >&2; echo "second line" >&2; exit 1'
mklauncher silent-capsule 'exit 1'
mklauncher old-capsule 'echo "capsule: error: unrecognized arguments: --only" >&2; exit 2'
check "a failing launcher's first stderr line is surfaced, not the rest" \
  'out="$(CAPSULE_BIN="$T/cp/noisy-capsule" "$CS" backend 2>&1)"; rc=$?
   [ $rc -eq 3 ] && grep -q "refusing host.json secrets.backend: odd launcher failure" <<<"$out" \
     && ! grep -q "second line" <<<"$out"'
check "a silent failing launcher says to reinstall it" \
  'out="$(CAPSULE_BIN="$T/cp/silent-capsule" "$CS" backend 2>&1)"; rc=$?
   [ $rc -eq 3 ] &&
   grep -q "reinstall the capsule launcher (it may predate host get --only)" <<<"$out"'
check "a launcher that predates --only says to reinstall it" \
  'out="$(CAPSULE_BIN="$T/cp/old-capsule" "$CS" backend 2>&1)"; rc=$?
   [ $rc -eq 3 ] && grep -q "reinstall the capsule launcher" <<<"$out"'
rm -f "$HJ" "$SF"
unset AGENT_OPS_NO_SEND

# --- callers go only through capsule-secret ---
check "slack.py, capsule-park.sh, hoi-poi.sh never name the infisical CLI" \
  '! grep -nE "(^|[^-/_a-z])infisical +(login|secrets|run|export)|INFISICAL_BIN" "$REPO/capsule/slack.py" "$REPO/bin/capsule-park.sh" "$REPO/bin/hoi-poi.sh"'
check "capsule-secret never puts a credential on argv" \
  '! grep -nE -- "--(token|client-secret|password)=" "$REPO/bin/capsule-secret"'

# Z1: contracts/intent/capsule_secret.jsonl replayed - each example names a check above that passed.
# A row whose document has `host_json` (object, or raw text) and/or `env` (object) is run:
# bin/capsule-secret <command> under a temp CAPSULE_HOME and CAPSULE_CONFIG_DIR, and exit,
# stdout (exact, if given) and stderr (substring) are compared. Other rows name a check above.
replay() {
  python3 - "$CS" "$1" "$PASSED" <<'PY'
import json, os, shlex, subprocess, sys, tempfile
cs, path, passed = sys.argv[1], sys.argv[2], sys.argv[3].split("|")
rows = [json.loads(line) for line in open(path) if line.strip()]
bad = 0 if rows else 1
for row in rows:
    doc, ex = row["given"]["document"], row["expect"]
    if "host_json" not in doc and "env" not in doc:
        bad += ex["pass"] not in passed
        continue
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as cfg:
        env = dict(os.environ, CAPSULE_HOME=home, CAPSULE_CONFIG_DIR=cfg, AGENT_OPS_NO_SEND="1")
        env.pop("CAPSULE_SECRETS_BACKEND", None)
        env.pop("CAPSULE_BIN", None)
        if "host_json" in doc:
            hj = doc["host_json"]
            with open(os.path.join(home, "host.json"), "w") as f:
                f.write(hj if isinstance(hj, str) else json.dumps(hj))
        env.update(doc.get("env", {}))
        p = subprocess.run([cs] + shlex.split(doc["command"]), env=env, timeout=60,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ok = p.returncode == ex.get("exit", 0) and ex.get("stderr", "") in p.stderr
    ok = ok and ("stdout" not in ex or p.stdout.strip() == ex["stdout"].strip())
    bad += not ok
sys.exit(1 if bad else 0)
PY
}
replay "$REPO/contracts/intent/capsule_secret.jsonl"; irc=$?
check "intent-examples" '[ $irc -eq 0 ]'
R="$T/replay.jsonl"
row() { printf '{"given":{"document":%s},"expect":%s}\n' "$1" "$2"; }
HJE='{"secrets":{"backend":"env"}}'
{ row "{\"host_json\":$HJE,\"command\":\"backend\"}" '{"exit":0,"stdout":"env"}'
  row '{"host_json":"{bad","command":"backend"}' '{"exit":3,"stderr":"host.json"}'
  row '{"env":{"CAPSULE_SECRETS_BACKEND":"file"},"command":"backend"}' \
    '{"exit":0,"stdout":"file"}'; } >"$R"
check "replay runs host_json and env rows" 'replay "$R"'
row "{\"host_json\":$HJE,\"command\":\"backend\"}" '{"exit":0,"stdout":"file"}' >"$R"
check "replay fails a row whose stdout differs" '! replay "$R"'
row '{"host_json":{},"command":"backend"}' '{"exit":3}' >"$R"
check "replay fails a row whose exit differs" '! replay "$R"'

echo; if [ $fails -eq 0 ]; then echo "test_capsule_secret_backends: all passed"; else echo "test_capsule_secret_backends: $fails failed"; exit 1; fi
