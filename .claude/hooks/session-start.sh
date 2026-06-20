#!/usr/bin/env bash
# SessionStart hook: make the repo runnable in a Claude Code web session that
# has no Nix. Best-effort and idempotent — it never fails the session (always
# exits 0), so a missing network or interpreter is a no-op rather than an error.
#
# On a normal dev box you use the Nix dev shell (`nix develop`) and this is a
# no-op. The fallback builds a local `.venv` with the dev tools so `pytest`,
# `ruff`, and `mypy` work, plus the `roomieorder` CLI for the diagnostics.
set -u
cd "$(dirname "$0")/../.." || exit 0

# Nix present → the flake dev shell is the supported path; nothing to do here.
command -v nix >/dev/null 2>&1 && exit 0

# Already bootstrapped.
[ -x .venv/bin/pytest ] && exit 0

PY="$(command -v python3.12 || command -v python3.13 || command -v python3 || true)"
[ -n "$PY" ] || exit 0

"$PY" -m venv .venv >/dev/null 2>&1 || exit 0
.venv/bin/pip install -q -e . pytest ruff mypy >/dev/null 2>&1 || true
exit 0
