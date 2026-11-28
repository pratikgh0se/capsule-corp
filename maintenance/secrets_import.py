#!/usr/bin/env python3
"""Idempotent bootstrap/import for the Capsule Corp secrets server."""

from __future__ import annotations

import json
import os
import secrets as secrets_mod
import stat
import sys
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from capsule import host  # noqa: E402

DEFAULT_BASE = "http://localhost:8085/api"
ORG_NAME = "Capsule Corp"
ENV_SLUG = "prod"
PROJECT_ROLE = "viewer"  # Infisical predefined read-only project role slug.

# The second slug, "agent-ops", names a live Infisical project distinct from this repo's
# name (only the "bridge" identity reads it) - not renamed with the repo rename
# (rename/capsule-corp inventory item B7).
def project_slugs():
    """Secret-store project slugs: host.json `secrets.projects` (default just capsule-corp)."""
    return list(host.get_or_default("secrets.projects"))



# identity name -> projects it needs read-only access to.
def identity_projects():
    return {
        "telegram-gateway": ["capsule-corp"],
        "bridge": project_slugs(),
        "merge-gate": ["capsule-corp"],
        "compose": ["capsule-corp"],
        "backup": ["capsule-corp"],
    }

# Identity the verify step (run()) authenticates as when it shells out to capsule-
# secret.
VERIFY_IDENTITY = "telegram-gateway"

DEFAULT_CONFIG_DIR = Path(os.environ.get("CAPSULE_CONFIG_DIR", str(Path.home() / ".config" / "capsule")))
# Defaults to the repo's helper, same as deploy.sh; no old-system home need exist.
CAPSULE_SECRET_BIN = os.environ.get(
    "CAPSULE_SECRET_BIN", str(Path(__file__).resolve().parent.parent / "bin" / "capsule-secret"))


OBSERVABILITY_KEYS = (
    "OPS_PG_PASSWORD",
    "OPS_GRAFANA_PASSWORD",
    "OPS_TG_BOT_TOKEN",
    "OPS_TG_CHAT_ID",
    "OPS_TG_THREAD_ID",
)


# --------------------------------------------------------------------------- _api: the
# one network primitive.

class ApiError(RuntimeError):
    """An HTTP error from the Infisical API; `status` is the numeric code."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def _api(method: str, path: str, body: Optional[dict] = None, token: Optional[str] = None,
         base: Optional[str] = None) -> dict:
    base = base or DEFAULT_BASE
    url = f"{base}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        # The response body under /v3/secrets/raw/ can echo back the secret value that
        # was just read or written (Infisical's own validation errors sometimes quote
        # it) - never put it in an exception message, which can end up in logs or on
        # stdout via main()'s error path.
        if path.startswith("/v3/secrets/raw/"):
            raise ApiError(f"{method} {path}: HTTP {e.code}", e.code)
        raise ApiError(f"{method} {path}: HTTP {e.code} {text[:120]}", e.code)
    except OSError as e:
        # urllib.error.URLError is itself an OSError (it wraps the socket error in
        # .reason), so this one clause also catches a bare TimeoutError/ConnectionError
        # raised during the read phase after a successful connect (e.g. Infisical is
        # listening but still running migrations) - the realistic shape of "the server
        # isn't ready yet", which callers (wait_for_status) must be able to retry on.
        raise RuntimeError(f"{method} {path}: {e}")
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        # Never quote the body: on the secrets endpoints it may hold a value.
        raise RuntimeError(f"{method} {path}: response was not JSON")


def wait_for_status(api: Callable, base: str, attempts: int = 60, delay: float = 2.0,
                     sleep: Callable = time.sleep) -> None:
    """Poll GET /status until it answers, backing off `delay` seconds between tries (60
    x 2s = 120s budget for a first-boot Infisical that runs DB migrations)."""
    last_err: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            api("GET", "/status", base=base)
            return
        except (RuntimeError, OSError) as e:
            last_err = e
            if attempt < attempts - 1:
                sleep(delay)
    raise RuntimeError(f"secrets_import: /status did not answer after {attempts} attempts: {last_err}")


def _default_cli(argv: list, env_extra: Optional[dict] = None) -> str:
    env = dict(os.environ, **env_extra) if env_extra else None
    # The verify step invokes capsule-secret, which itself requires CAPSULE_IDENTITY on
    # its environment (the helper no longer defaults it).
    if os.path.basename(argv[0]) == "capsule-secret":
        env = dict(os.environ)
        env.setdefault("CAPSULE_IDENTITY", VERIFY_IDENTITY)
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env)
    except FileNotFoundError:
        raise RuntimeError(f"{argv[0]}: not found (set CAPSULE_SECRET_BIN)") from None
    if result.returncode != 0:
        # Never include stderr here: the one command run through `cli` with a secret on
        # its environment is `infisical bootstrap` (INFISICAL_ADMIN_PASSWORD, see
        # ensure_bootstrap) - a CLI that echoes a validation message on a bad
        # password would put that password into this exception, which main()'s
        # error path prints to stdout.
        raise RuntimeError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''}: exit {result.returncode}")
    return result.stdout


# --------------------------------------------------------------------------- Sources
# ---------------------------------------------------------------------------

def _parse_env_file(path: Path, keys) -> dict:
    out = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in keys:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def read_sources(sources: Optional[dict] = None) -> dict:
    """Build {name: value} from the two known env files plus the canary."""
    cfg = host.config_dir()    # the jobs config dir: ops.env and observability.env live there
    files = {"observability": cfg / "observability.env", "ops": cfg / "ops.env"}
    if sources:
        files.update(sources)

    out = {}
    out.update(_parse_env_file(Path(files["observability"]), OBSERVABILITY_KEYS))
    # ops.env: everything OPS_* it defines is kept, non-secret but useful for completeness.
    ops_raw = {}
    try:
        text = Path(files["ops"]).read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if not key.startswith("OPS_"):
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            ops_raw[key] = value
    except OSError:
        pass
    out.update(ops_raw)
    out["CAPSULE_CHECK"] = "ok"
    return out


# --------------------------------------------------------------------------- Config
# file writers (0600 files, 0700 dirs)
# ---------------------------------------------------------------------------

def _write_secure(path: Path, content: str, config_dir: Optional[Path] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Path.mkdir(mode=...) applies the mode only to the leaf directory it creates; any
    # parents it also has to create along the way (e.g. identities/ implying
    # config_dir/) get 0o777 & ~umask.
    d = path.parent
    stop = Path(config_dir) if config_dir is not None else d
    while True:
        os.chmod(d, 0o700)
        if d == stop or d.parent == d:
            break
        d = d.parent
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def _slug_upper(slug: str) -> str:
    return slug.upper().replace("-", "_")


def write_projects_env(config_dir: Path, projects: dict) -> Path:
    """Merge `projects` into the file: entries for projects not named here are kept."""
    path = config_dir / "projects.env"
    try:
        old = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        old = []
    new = {f"PROJECT_{_slug_upper(slug)}": pid for slug, pid in projects.items()}
    kept = [ln for ln in old if ln.strip() and ln.split("=", 1)[0] not in new]
    lines = kept + [f"{k}={v}" for k, v in new.items()]
    _write_secure(path, "\n".join(lines) + "\n", config_dir=config_dir)
    return path


def write_identity_env(config_dir: Path, name: str, client_id: str, client_secret: str) -> Path:
    path = config_dir / "identities" / f"{name}.env"
    content = f"INFISICAL_CLIENT_ID={client_id}\nINFISICAL_CLIENT_SECRET={client_secret}\n"
    _write_secure(path, content, config_dir=config_dir)
    return path


def read_admin_env(config_dir: Path) -> dict:
    path = config_dir / "admin.env"
    out = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------------------- Bootstrap
# ---------------------------------------------------------------------------

def _merge_admin_env(config_dir: Path, updates: dict) -> dict:
    """Merge `updates` into admin.env (creating it if absent), preserving
    every key already there. Returns the merged dict.
    """
    admin = read_admin_env(config_dir)
    admin.update(updates)
    lines = [f"{k}={v}" for k, v in admin.items()]
    _write_secure(config_dir / "admin.env", "\n".join(lines) + "\n", config_dir=config_dir)
    return admin


def resolve_admin_email(admin: dict) -> str:
    """Instance admin email: CAPSULE_ADMIN_EMAIL, else the one saved in admin.env on the
    first run, else `git config user.email`. No default is shipped in the repo."""
    email = os.environ.get("CAPSULE_ADMIN_EMAIL") or admin.get("INFISICAL_ADMIN_EMAIL") or ""
    email = email.strip()
    if not email:
        try:
            email = subprocess.run(["git", "config", "user.email"], capture_output=True,
                                   text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            email = ""
    if not email:
        raise RuntimeError("secrets_import: admin email unknown; set CAPSULE_ADMIN_EMAIL "
                           "(or git config --global user.email)")
    return email


def ensure_bootstrap(cli: Callable, config_dir: Path, base: str, api: Optional[Callable] = None) -> dict:
    """Run `infisical bootstrap`, returning {"token":, "org_id":, "email":}."""
    api = api or _api
    admin = read_admin_env(config_dir)
    admin_email = resolve_admin_email(admin)
    password = admin.get("INFISICAL_ADMIN_PASSWORD") or secrets_mod.token_urlsafe(24)

    argv = [
        "infisical", "bootstrap",
        "--domain", base,
        "--email", admin_email,
        "--organization", ORG_NAME,
        "--ignore-if-bootstrapped",
        "--output", "json",
    ]
    # The password goes in the env var the CLI reads, never on argv (visible in ps).
    raw = cli(argv, env_extra={"INFISICAL_ADMIN_PASSWORD": password})
    boot_token = None
    identity_id = None
    org_id = None
    try:
        parsed = json.loads(raw) if raw and raw.strip() else {}
        boot_token = (parsed.get("identity") or {}).get("credentials", {}).get("token")
        identity_id = (parsed.get("identity") or {}).get("id")
        org_id = (parsed.get("organization") or {}).get("id")
    except json.JSONDecodeError:
        parsed = {}

    if not org_id:
        org_id = admin.get("INFISICAL_ORG_ID")

    is_first_run = not admin.get("INSTANCE_ADMIN_TOKEN")

    if is_first_run:
        if not boot_token or not org_id:
            raise RuntimeError("secrets_import: bootstrap produced no usable admin token/org id")
        admin = _merge_admin_env(config_dir, {
            "INFISICAL_ADMIN_EMAIL": admin_email,
            "INFISICAL_ADMIN_PASSWORD": password,
            "INSTANCE_ADMIN_TOKEN": boot_token,
            "INFISICAL_ORG_ID": org_id,
        })

        if identity_id:
            attach = api("POST", f"/v1/auth/universal-auth/identities/{identity_id}",
                         body={}, token=boot_token, base=base)
            client_id = attach["identityUniversalAuth"]["clientId"]
            secret = api("POST", f"/v1/auth/universal-auth/identities/{identity_id}/client-secrets",
                         body={}, token=boot_token, base=base)
            client_secret = secret["clientSecret"]
            admin = _merge_admin_env(config_dir, {
                "INFISICAL_ADMIN_CLIENT_ID": client_id,
                "INFISICAL_ADMIN_CLIENT_SECRET": client_secret,
            })

    client_id = admin.get("INFISICAL_ADMIN_CLIENT_ID")
    client_secret = admin.get("INFISICAL_ADMIN_CLIENT_SECRET")
    if client_id and client_secret:
        login = api("POST", "/v1/auth/universal-auth/login",
                    body={"clientId": client_id, "clientSecret": client_secret}, base=base)
        token = login["accessToken"]
    else:
        # Legacy admin.env from before this fix: no client credentials exist, so the
        # only usable token is the (possibly stale) INSTANCE_ADMIN_TOKEN.
        token = boot_token or admin.get("INSTANCE_ADMIN_TOKEN")
        if not token or not org_id:
            raise RuntimeError("secrets_import: bootstrap produced no usable admin token/org id")
        try:
            api("GET", f"/v2/organizations/{org_id}/identity-memberships", token=token, base=base)
        except ApiError as e:
            if e.status == 401:
                raise RuntimeError(
                    "secrets_import: admin token expired and no admin client credentials in "
                    "admin.env: re-bootstrap the secrets admin"
                )
            raise

    return {"token": token, "org_id": org_id, "email": admin_email}


# --------------------------------------------------------------------------- Projects
# ---------------------------------------------------------------------------

def ensure_project(api: Callable, token: str, base: str, slug: str) -> str:
    # Only the lookup goes in the try: a 404 means "no such project, create it"; any
    # other failure (500, network) must propagate, not be treated as "doesn't exist".
    project_id = None
    envs = set()
    try:
        resp = api("GET", f"/v2/workspace/{slug}", token=token, base=base)
        project_id = resp["id"]
        envs = {e.get("slug") for e in resp.get("environments", [])}
    except RuntimeError as e:
        if getattr(e, "status", None) != 404:
            raise

    if project_id is None:
        resp = api("POST", "/v2/workspace",
                   body={"projectName": slug, "slug": slug, "shouldCreateDefaultEnvs": False},
                   token=token, base=base)
        project = resp.get("project", resp)
        project_id = project["id"]
        api("POST", f"/v1/workspace/{project_id}/environments",
            body={"name": "Production", "slug": ENV_SLUG}, token=token, base=base)
        return project_id

    if ENV_SLUG not in envs:
        api("POST", f"/v1/workspace/{project_id}/environments",
            body={"name": "Production", "slug": ENV_SLUG}, token=token, base=base)
    return project_id


# --------------------------------------------------------------------------- Identities
# ---------------------------------------------------------------------------

def ensure_identity(api: Callable, token: str, base: str, org_id: str, name: str):
    """Returns (identity_id, client_id_or_None, client_secret_or_None)."""
    resp = api("GET", f"/v2/organizations/{org_id}/identity-memberships", token=token, base=base)
    for m in resp.get("identityMemberships", []):
        identity = m.get("identity", {})
        if identity.get("name") == name:
            return identity.get("id", m.get("identityId")), None, None

    created = api("POST", "/v1/identities",
                  body={"name": name, "organizationId": org_id, "role": "no-access"},
                  token=token, base=base)
    identity_id = created["identity"]["id"]

    attach = api("POST", f"/v1/auth/universal-auth/identities/{identity_id}", body={}, token=token, base=base)
    client_id = attach["identityUniversalAuth"]["clientId"]

    secret = api("POST", f"/v1/auth/universal-auth/identities/{identity_id}/client-secrets",
                 body={}, token=token, base=base)
    client_secret = secret["clientSecret"]

    return identity_id, client_id, client_secret


def mint_client_secret(api: Callable, token: str, base: str, identity_id: str):
    """Mint a fresh client secret for an identity whose universal-auth is already
    attached (repairs a run interrupted after ensure_identity created
    the identity but before its env file was written, or a file deleted by hand)."""
    attach = api("GET", f"/v1/auth/universal-auth/identities/{identity_id}", token=token, base=base)
    client_id = attach["identityUniversalAuth"]["clientId"]
    secret = api("POST", f"/v1/auth/universal-auth/identities/{identity_id}/client-secrets",
                 body={}, token=token, base=base)
    return client_id, secret["clientSecret"]


def ensure_membership(api: Callable, token: str, base: str, project_id: str, identity_id: str) -> None:
    # Only the lookup goes in the try: a 404 means "not a member yet, add it"; any other
    # failure (500, network) must propagate rather than be read as "not a member" and
    # trigger a blind membership POST (the OpenAPI spec for this GET does
    # not document a 403 that specifically means "not a member", only a generic auth-
    # failure shape shared with every other endpoint, so only 404 is treated as absent).
    try:
        api("GET", f"/v2/workspace/{project_id}/identity-memberships/{identity_id}", token=token, base=base)
        return
    except ApiError as e:
        if e.status != 404:
            raise
    api("POST", f"/v2/workspace/{project_id}/identity-memberships/{identity_id}",
        body={"role": PROJECT_ROLE}, token=token, base=base)


# --------------------------------------------------------------------------- Secrets
# ---------------------------------------------------------------------------

def _secret_query(project_id: str, name: str) -> str:
    params = urllib.parse.urlencode({
        "workspaceId": project_id,
        "environment": ENV_SLUG,
        "secretPath": "/",
        "viewSecretValue": "true",
    })
    return f"/v3/secrets/raw/{name}?{params}"


def get_secret(api: Callable, token: str, base: str, project_id: str, name: str) -> Optional[str]:
    # Only a 404 means "no such secret yet": a plain RuntimeError
    # (network) or a non-404 ApiError must propagate, since put_secret treats None as
    # "create" - reading a transient 500 as "does not exist" turns it into a spurious
    # create POST against a secret that may already exist.
    try:
        resp = api("GET", _secret_query(project_id, name), token=token, base=base)
    except ApiError as e:
        if e.status != 404:
            raise
        return None
    return resp.get("secret", {}).get("secretValue")


def put_secret(api: Callable, token: str, base: str, project_id: str, name: str, value: str) -> bool:
    """Returns True iff a write happened (create or update)."""
    current = get_secret(api, token, base, project_id, name)
    if current == value:
        return False
    body = {"workspaceId": project_id, "environment": ENV_SLUG, "secretPath": "/", "secretValue": value}
    if current is None:
        api("POST", f"/v3/secrets/raw/{name}", body=body, token=token, base=base)
    else:
        api("PATCH", f"/v3/secrets/raw/{name}", body=body, token=token, base=base)
    return True


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run(*, api=None, cli=None, config_dir=None, sources=None, base=None, sleep=None) -> dict:
    api = api or _api
    cli = cli or _default_cli
    base = base or DEFAULT_BASE
    config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    wait_for_status(api, base, sleep=sleep or time.sleep)
    source_values = read_sources(sources)

    admin_exists_before = (config_dir / "admin.env").exists()
    boot = ensure_bootstrap(cli, config_dir, base, api=api)
    token, org_id = boot["token"], boot["org_id"]

    projects = {}
    for slug in project_slugs():
        projects[slug] = ensure_project(api, token, base, slug)
    write_projects_env(config_dir, projects)

    identities = []
    for name, needed_projects in identity_projects().items():
        identity_id, client_id, client_secret = ensure_identity(api, token, base, org_id, name)
        if client_id and client_secret:
            write_identity_env(config_dir, name, client_id, client_secret)
        elif not (config_dir / "identities" / f"{name}.env").exists():
            # the identity already existed (ensure_identity returned no
            # fresh secret) but a prior run never got as far as writing its env file -
            # e.g. it was interrupted between creating the five identities and writing
            # them, or the file was deleted by hand.
            client_id, client_secret = mint_client_secret(api, token, base, identity_id)
            write_identity_env(config_dir, name, client_id, client_secret)
        for slug in needed_projects:
            ensure_membership(api, token, base, projects[slug], identity_id)
        identities.append(name)

    imported = []
    capsule_corp_id = projects["capsule-corp"]
    for name, value in source_values.items():
        if put_secret(api, token, base, capsule_corp_id, name, value):
            imported.append(name)

    verified = False
    try:
        # Resolved explicitly rather than a bare "capsule-secret": the helper
        # lives in the repo's bin/, but this script is run by hand and cannot assume
        # that directory is on PATH (Minor 4) - a "verified no" here must mean "the
        # value differed", not "PATH".
        out = cli([CAPSULE_SECRET_BIN, "get", "capsule-corp", "OPS_PG_PASSWORD"]).strip()
        expected = source_values.get("OPS_PG_PASSWORD")
        verified = expected is not None and out == expected
    except RuntimeError as exc:
        verified = False
        print(f"secrets: verify step failed: {exc}")

    print(
        f"secrets: imported {len(imported)} ({', '.join(imported)}), "
        f"identities {len(identities)}, projects {len(projects)}, "
        f"verified {'yes' if verified else 'no'}"
    )

    return {
        "bootstrapped": not admin_exists_before,
        "projects": projects,
        "identities": identities,
        "imported": imported,
        "verified": verified,
    }


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Bootstrap and import Capsule Corp secrets")
    parser.add_argument("--base", default=None, help=f"API base (default {DEFAULT_BASE})")
    parser.add_argument("--config-dir", default=None, help="Config dir (default ~/.config/capsule)")
    args = parser.parse_args(argv)

    # Exit codes (documented in the RUNBOOK's Secrets step 5): 0 - ran to
    # completion and its own verify step confirmed the imported value reads back through
    # capsule-secret 1 - ran to completion but verify failed (no exception - e.g.
    # capsule-secret was not on PATH, or the identity file/role is wrong) - the whole
    # point of an unattended run proving itself 2 - raised (bootstrap/API/network
    # failure) before it could even try
    try:
        result = run(base=args.base, config_dir=args.config_dir)
    except (RuntimeError, OSError) as e:
        print(f"secrets_import: {e}")
        return 2
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
