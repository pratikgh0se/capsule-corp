"""Runner tiers: strong (docker), host (read-only), light (seatbelt), mac (seatbelt + Xcode, D305)
(6.6, rows 373/378/395)."""


def _blind_repo(cfg):
    """The board's blind repo path (expanded), or None when it has no `blind_tests` block (R33)."""
    import os

    from capsule import config
    block = config.blind_tests(cfg)
    repo = (block or {}).get("repo")
    return os.path.realpath(os.path.expanduser(repo)) if repo else None


def scoped(factory, cfg_of, home=None):
    """Wrap a runner factory so each runner it returns carries the tick's board, and a runner
    with an `enabled` switch (the light and Mac tiers) the board's `redesign.<its own switch>`
    (`light_runner`, `mac_runner`) and the tick's
    own capsule `home` (whose spools its reap reads), all from the board config `cfg_of()` (None
    before the first load: no board, switches off)."""
    def make(tier, role):
        from capsule import config, paths
        runner = factory(tier, role)
        runner.board = (cfg_of() or {}).get("board")
        runner.blind_repo = _blind_repo(cfg_of())
        runner.git_identity = (cfg_of() or {}).get("git_identity")
        runner.spool_root = paths.board_dir(runner.board, home) / "spool" if runner.board else None
        runner.home = home
        # the tester's clones
        runner.blind_work = runner.spool_root and runner.spool_root.parent / "blind-work"
        if hasattr(runner, "enabled"):
            runner.enabled = config.redesign(cfg_of())[getattr(runner, "switch", "light_runner")]
        return runner
    return make


def reap(factory, tiers, live_run_ids):
    """Reap the leftovers of every up tier (`tiers`): strong containers, light and Mac processes.
    One
    runner object serving both tiers (a test double) is reaped once. Returns what was reaped. A
    tier whose reap raises does not stop the next one: the first error is raised at the end."""
    runners, reaped, error = [], [], None
    for tier in ("strong", "light", "mac"):
        runner = factory(tier, "implementer") if tier in tiers else None
        # the light and Mac runners share one reap (same spool scan): once per pass
        if runner is not None and not any(
                runner is r or (tier == "mac" and getattr(r, "tier", None) == "light"
                                and type(r).reap is type(runner).reap)
                for r in runners):
            runners.append(runner)
    for runner in runners:
        try:
            reaped += runner.reap(live_run_ids)
        except Exception as exc:
            error = error or exc
    if error:
        raise error
    return reaped


def runner_for(tier, role):
    """Pick the runner for a (tier, role) pair.

    strong -> docker.StrongRunner for roles that write code (implementer);
    read-only roles (reviewer, validator, guide) always get host.HostRunner
    regardless of tier, since they never need a container.
    light -> host.LightRunner (unavailable unless `redesign.light_runner` is on, see `scoped`).
    mac -> mac.MacRunner for the implementer (unavailable unless `redesign.mac_runner` is on and
    the host can run it); a mac card is never given docker or the light runner.
    """
    from capsule.runners import docker, host

    # korin: read-only post-mortem (row 456); security: BLD-02
    if role in ("reviewer", "validator", "guide", "korin", "security"):
        return host.HostRunner()
    if tier == "mac" and role != "blind-tester":    # the tester's container is the strong tier's
        return host.HostRunner._seatbelt_runner("mac")
    if tier == "light":
        return host.HostRunner._seatbelt_runner("light")
    return docker.StrongRunner()
