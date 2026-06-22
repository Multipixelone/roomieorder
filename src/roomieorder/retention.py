"""Screenshot/DOM-dump retention — keep the shots dir from filling the disk.

The buy flow writes a PNG (and, for ``dump-dom``, an HTML + probe ``.txt``) on
every attempt into ``shots_dir`` — the systemd ``StateDirectory`` (mode 0700) on
the deployment — with no rotation, so it grows unbounded. The worker prunes at
startup and after each order; ``roomieorder prune-shots`` runs it by hand. Pure
filesystem, no browser/DB, so it's cheap to call often and safe in the CLI.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

_logger = logging.getLogger(__name__)

_SECONDS_PER_DAY = 86_400.0


def prune_shots(shots_dir: Path, retention_days: int) -> int:
    """Delete files in ``shots_dir`` older than ``retention_days``; return the count.

    A no-op (returns 0) when ``retention_days <= 0`` (pruning disabled) or the
    directory doesn't exist yet. Only top-level files are considered — the shots
    dir is flat — keyed on mtime. Best-effort per file: an unremovable entry is
    logged and skipped so one bad file never aborts the sweep or the worker loop.
    """
    if retention_days <= 0 or not shots_dir.exists():
        return 0
    cutoff = time.time() - retention_days * _SECONDS_PER_DAY
    removed = 0
    for path in shots_dir.iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
        except OSError as exc:  # noqa: PERF203 — per-file best effort
            _logger.warning("prune: couldn't remove %s: %s", path, exc)
    if removed:
        _logger.info("pruned %d shot(s) older than %dd from %s", removed, retention_days, shots_dir)
    return removed
