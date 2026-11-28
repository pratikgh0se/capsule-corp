# Brewfile: only what `capsule doctor` requires and Homebrew can install. Names checked on
# formulae.brew.sh on 2026-10-29. All three sources are the default homebrew/core and
# homebrew/cask taps; there are no third-party taps.
# Python 3.11+ comes through uv (`uv python install 3.11`); doctor's fix text says the same.
# `claude-code@latest` is the cask doctor names (the plain `claude-code` cask conflicts with it).
# `git` because doctor needs git 2.40+ (macOS ships an older one).
# `orbstack` is the default Docker engine (decision D320); Docker Desktop is the alternative (CAPSULE_DOCKER_ENGINE=desktop).
# OrbStack must also be started once (open -a OrbStack); the Claude login is yours to do.
brew "uv"
brew "gh"
brew "git"
cask "claude-code@latest"
cask "orbstack"
