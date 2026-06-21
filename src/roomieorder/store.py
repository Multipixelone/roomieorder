"""SQLite-backed queue + guard bookkeeping.

The queue is the seam between the two halves of the app: intake inserts
``pending`` rows and returns instantly; the worker drains them. All guard
state (debounce, cooldown, daily spend, worker-pause) is derived from this one
table plus a tiny key/value ``worker_state`` table, so there is a single source
of truth and no cross-table drift.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel

# Terminal + transient statuses a queue row can hold. Mirrors the Sheets
# `status` column (PLAN §3.5) plus the internal `pending`/`in_progress`.
Status = Literal[
    "pending",
    "in_progress",
    "placed",
    "dry_run",
    "skipped_cooldown",
    "skipped_debounce",
    "price_blocked",
    "spend_capped",
    # Product is sold out / not carried / not found at the store. On the Costco
    # leg this triggers the Amazon fallback; on the last provider it's terminal.
    "unavailable",
    "failed",
    # Place Order was clicked but the confirmation couldn't be read, so the order
    # *may* have gone through. Never auto-retried (that risks a double order) —
    # the worker pauses and a human confirms against the store account.
    "needs_review",
    "challenge",
    # Akamai hard block (Access Denied) — a fingerprint/IP ban with nothing to
    # click. Pauses the worker like `challenge` and is terminal (no fallback);
    # split out so the operator message says "wait it out" not "clear it".
    "blocked",
]

# Statuses that represent a real, completed money movement for spend accounting.
SPEND_STATUSES = ("placed",)

# How many times a row may be claimed before it's treated as exhausted. The buy
# is a money-moving step that may have *already placed* an order when it died, so
# the safe policy is one attempt: never auto-retry a row that reached the worker,
# hand it to the operator instead (see ``recover_stale`` and ``claim_next_pending``).
MAX_ATTEMPTS = 1


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class QueueRow(BaseModel):
    id: int
    item_key: str
    requester: str
    status: Status
    created_at: datetime
    updated_at: datetime
    attempts: int = 0
    unit_price: Optional[float] = None
    order_total: Optional[float] = None
    order_id: Optional[str] = None
    # Which store fulfilled (or last attempted) the order: "costco"/"amazon".
    provider: str = ""
    notes: str = ""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_key    TEXT    NOT NULL,
    requester   TEXT    NOT NULL DEFAULT 'household',
    status      TEXT    NOT NULL DEFAULT 'pending',
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    unit_price  REAL,
    order_total REAL,
    order_id    TEXT,
    provider    TEXT    NOT NULL DEFAULT '',
    notes       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_queue_status  ON queue(status);
CREATE INDEX IF NOT EXISTS idx_queue_item    ON queue(item_key, created_at);

CREATE TABLE IF NOT EXISTS worker_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    """Thin synchronous wrapper around the SQLite state file.

    One connection per Store instance, shared by the worker thread and uvicorn's
    (async) intake threadpool. ``sqlite3`` serialises individual statements, but
    ``commit()`` is connection-global — one thread's commit flushes another
    thread's half-finished write — so every method runs under ``self._lock``.
    That serialises whole operations, making a method's commit atomic relative to
    the other threads and keeping any future multi-statement transaction safe.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: uvicorn's threadpool may touch the same
        # connection. The reentrant lock below — not SQLite's per-statement
        # serialisation — is what gives commit isolation between the threads.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    # ─────────── schema ───────────

    def init_db(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Bring an existing DB up to the current schema, idempotently.

        ``CREATE TABLE IF NOT EXISTS`` never alters a table that already exists,
        so columns added after a deployment's first boot need an explicit guarded
        ``ALTER TABLE`` here. Re-runnable: each add is skipped when the column is
        already present.
        """
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(queue)")}
        if "provider" not in cols:
            self._conn.execute(
                "ALTER TABLE queue ADD COLUMN provider TEXT NOT NULL DEFAULT ''"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ─────────── enqueue / drain ───────────

    def enqueue(self, item_key: str, requester: str = "household") -> int:
        now = _iso(_utcnow())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO queue (item_key, requester, status, created_at, updated_at) "
                "VALUES (?, ?, 'pending', ?, ?)",
                (item_key, requester, now, now),
            )
            self._conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def claim_next_pending(self) -> Optional[QueueRow]:
        """Atomically take the oldest pending row, flipping it to in_progress.

        Returns None when the queue is empty (or every pending row has already
        exhausted ``MAX_ATTEMPTS``). The UPDATE…RETURNING is a single statement so
        two workers (or a restart mid-drain) can't claim the same row twice. The
        ``attempts < MAX_ATTEMPTS`` guard enforces the retry cap: a row that's
        been claimed up to the cap is left for ``recover_stale`` to fail rather
        than re-run a possibly-placed order.
        """
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE queue SET status='in_progress', updated_at=?, attempts=attempts+1
                WHERE id = (
                    SELECT id FROM queue WHERE status='pending' AND attempts < ?
                    ORDER BY created_at ASC LIMIT 1
                )
                RETURNING *
                """,
                (_iso(_utcnow()), MAX_ATTEMPTS),
            )
            row = cur.fetchone()
            self._conn.commit()
        return self._to_row(row) if row else None

    def recover_stale(self) -> list[QueueRow]:
        """Resolve rows left ``in_progress`` by a hard restart.

        ``claim_next_pending`` only ever selects ``pending`` rows, so a process
        death (SIGKILL, OOM, power loss, a systemd restart) between the claim and
        the terminal ``mark`` strands the row ``in_progress`` forever — never
        re-claimed, never failed, never surfaced. Run once at startup: a row that
        already reached ``MAX_ATTEMPTS`` becomes ``failed`` (it reached the buy
        and may have *placed* an order — a human must check, never auto-retry);
        one still under the cap goes back to ``pending`` for another drain.

        Returns the rows that were *failed* (the ones needing review), so the
        caller can pause the worker and notify the operator. Idempotent: a second
        call finds no ``in_progress`` rows and returns an empty list.
        """
        note = "recovered: stuck in_progress after a restart — may have been placed, needs review"
        with self._lock:
            stale = self._conn.execute(
                "SELECT id, attempts FROM queue WHERE status='in_progress'"
            ).fetchall()
            now = _iso(_utcnow())
            failed_ids: list[int] = []
            for r in stale:
                if r["attempts"] >= MAX_ATTEMPTS:
                    self._conn.execute(
                        "UPDATE queue SET status='failed', updated_at=?, notes=? WHERE id=?",
                        (now, note, r["id"]),
                    )
                    failed_ids.append(r["id"])
                else:
                    self._conn.execute(
                        "UPDATE queue SET status='pending', updated_at=? WHERE id=?",
                        (now, r["id"]),
                    )
            self._conn.commit()
            if not failed_ids:
                return []
            placeholders = ",".join("?" for _ in failed_ids)
            rows = self._conn.execute(
                f"SELECT * FROM queue WHERE id IN ({placeholders}) ORDER BY id", failed_ids
            ).fetchall()
        return [self._to_row(r) for r in rows]

    def mark(
        self,
        row_id: int,
        status: Status,
        *,
        unit_price: Optional[float] = None,
        order_total: Optional[float] = None,
        order_id: Optional[str] = None,
        provider: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> None:
        """Set a row's terminal status and any scraped fields.

        Only non-None fields are written, so a price-block can record the
        unit_price it tripped on without clobbering order columns.
        """
        sets = ["status=?", "updated_at=?"]
        args: list[object] = [status, _iso(_utcnow())]
        for col, val in (
            ("unit_price", unit_price),
            ("order_total", order_total),
            ("order_id", order_id),
            ("provider", provider),
            ("notes", notes),
        ):
            if val is not None:
                sets.append(f"{col}=?")
                args.append(val)
        args.append(row_id)
        with self._lock:
            self._conn.execute(f"UPDATE queue SET {', '.join(sets)} WHERE id=?", args)
            self._conn.commit()

    # ─────────── guard queries ───────────

    def last_request_at(self, item_key: str) -> Optional[datetime]:
        """Most recent *enqueue* time for an item_key (debounce check)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at FROM queue WHERE item_key=? ORDER BY created_at DESC LIMIT 1",
                (item_key,),
            ).fetchone()
        return _parse(row["created_at"]) if row else None

    def last_placed_at(self, item_key: str) -> Optional[datetime]:
        """Most recent successfully *placed* order time (cooldown check)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT updated_at FROM queue WHERE item_key=? AND status='placed' "
                "ORDER BY updated_at DESC LIMIT 1",
                (item_key,),
            ).fetchone()
        return _parse(row["updated_at"]) if row else None

    def last_placed_at_all(self) -> dict[str, datetime]:
        """Most recent *placed* time for every item_key, in a single query.

        The dashboard poll (`GET /items`) needs the cooldown clock for the whole
        catalog at once; this grouped read replaces one ``last_placed_at`` query
        per item. Item keys with no placed order simply don't appear in the map."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_key, MAX(updated_at) AS ts FROM queue "
                "WHERE status='placed' GROUP BY item_key"
            ).fetchall()
        return {r["item_key"]: _parse(r["ts"]) for r in rows}

    def spend_since(self, hours: float = 24.0) -> float:
        """Sum of order_total for placed orders in the trailing window."""
        cutoff = _iso(_utcnow() - timedelta(hours=hours))
        placeholders = ",".join("?" for _ in SPEND_STATUSES)
        with self._lock:
            row = self._conn.execute(
                f"SELECT COALESCE(SUM(order_total), 0.0) AS total FROM queue "
                f"WHERE status IN ({placeholders}) AND updated_at >= ? "
                f"AND order_total IS NOT NULL",
                (*SPEND_STATUSES, cutoff),
            ).fetchone()
        return float(row["total"])

    def list_queue(self, limit: int = 20) -> list[QueueRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM queue ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._to_row(r) for r in rows]

    def pending_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM queue WHERE status='pending'"
            ).fetchone()
        return int(row["n"])

    # ─────────── worker pause ───────────

    def set_paused(self, paused: bool, reason: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO worker_state (key, value) VALUES ('paused', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if paused else "0",),
            )
            self._conn.execute(
                "INSERT INTO worker_state (key, value) VALUES ('pause_reason', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (reason,),
            )
            self._conn.commit()

    def is_paused(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM worker_state WHERE key='paused'"
            ).fetchone()
        return bool(row and row["value"] == "1")

    def pause_reason(self) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM worker_state WHERE key='pause_reason'"
            ).fetchone()
        return row["value"] if row else ""

    # ─────────── internals ───────────

    @staticmethod
    def _to_row(row: sqlite3.Row) -> QueueRow:
        return QueueRow(
            id=row["id"],
            item_key=row["item_key"],
            requester=row["requester"],
            status=row["status"],
            created_at=_parse(row["created_at"]),
            updated_at=_parse(row["updated_at"]),
            attempts=row["attempts"],
            unit_price=row["unit_price"],
            order_total=row["order_total"],
            order_id=row["order_id"],
            provider=row["provider"] or "",
            notes=row["notes"] or "",
        )
