from __future__ import annotations

import os
import time
from pathlib import Path

from roomieorder.retention import prune_shots


def _touch(path: Path, age_days: float) -> Path:
    path.write_bytes(b"x")
    when = time.time() - age_days * 86_400
    os.utime(path, (when, when))
    return path


def test_prune_removes_only_old_files(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    shots.mkdir()
    old = _touch(shots / "20260101T000000Z_costco_paper_towels_review.png", age_days=40)
    fresh = _touch(shots / "20260620T000000Z_costco_paper_towels_review.png", age_days=2)

    removed = prune_shots(shots, retention_days=30)

    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_prune_disabled_when_retention_zero(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    shots.mkdir()
    old = _touch(shots / "old.png", age_days=999)
    assert prune_shots(shots, retention_days=0) == 0
    assert old.exists()


def test_prune_noop_when_dir_missing(tmp_path: Path) -> None:
    assert prune_shots(tmp_path / "nope", retention_days=30) == 0


def test_prune_covers_dom_and_probe_artifacts(tmp_path: Path) -> None:
    # dump-dom writes *_dom.html and *_probe.txt alongside PNGs; all should prune.
    shots = tmp_path / "shots"
    shots.mkdir()
    _touch(shots / "old_dom.html", age_days=40)
    _touch(shots / "old_probe.txt", age_days=40)
    _touch(shots / "old_shot.png", age_days=40)
    assert prune_shots(shots, retention_days=30) == 3
    assert list(shots.iterdir()) == []
