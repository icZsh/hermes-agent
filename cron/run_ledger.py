"""SQLite-backed run ledger for cron scheduler decisions."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home
from hermes_time import get_timezone, now as _hermes_now

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = frozenset({"success", "failed", "blocked", "skipped", "ready_degraded"})
DEFAULT_KEEP_PER_JOB = 200
DEFAULT_KEEP_DAYS = 90


class RunLedgerUnavailable(RuntimeError):
    """Raised when the run ledger cannot be opened or written."""


def default_runs_db_path() -> Path:
    """Return the default cron run ledger path under the active Hermes home."""
    return get_hermes_home() / "cron" / "runs.db"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime | str | None) -> str | None:
    parsed = _as_utc(value)
    if parsed is None:
        return None
    return parsed.isoformat().replace("+00:00", "Z")


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def compute_logical_date(scheduled_for: datetime | str | None) -> str:
    """Return the Hermes-local logical date for a scheduled timestamp."""
    scheduled_at = _as_utc(scheduled_for) or _hermes_now()
    tz = get_timezone()
    if tz is not None:
        return scheduled_at.astimezone(tz).date().isoformat()
    return scheduled_at.astimezone().date().isoformat()


class RunLedger:
    """Persistent ledger of cron run attempts and gate decisions."""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path is not None else default_runs_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        try:
            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                timeout=5.0,
                isolation_level=None,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_schema()
        except sqlite3.Error as exc:
            raise RunLedgerUnavailable(f"Run ledger unavailable at {self.db_path}: {exc}") from exc
        except OSError as exc:
            raise RunLedgerUnavailable(f"Run ledger path unavailable at {self.db_path}: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
              run_id                 TEXT PRIMARY KEY,
              job_id                 TEXT NOT NULL,
              scheduled_for          TEXT,
              logical_date           TEXT,
              trigger_type           TEXT,
              parent_run_id          TEXT,
              attempt                INTEGER,
              started_at             TEXT,
              finished_at            TEXT,
              status                 TEXT,
              reason_code            TEXT,
              reason_detail          TEXT,
              upstream_snapshot      TEXT,
              output_file            TEXT,
              dependency_recheck_at  TEXT,
              scheduler_instance_id  TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_job_sched
              ON runs (job_id, scheduled_for);
            CREATE INDEX IF NOT EXISTS idx_job_status
              ON runs (job_id, status, finished_at DESC);
            CREATE INDEX IF NOT EXISTS idx_job_logical
              ON runs (job_id, logical_date);
            CREATE INDEX IF NOT EXISTS idx_recheck
              ON runs (status, dependency_recheck_at);
            """
        )

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        try:
            with self._lock:
                return self._conn.execute(sql, tuple(params))
        except sqlite3.Error as exc:
            raise RunLedgerUnavailable(f"Run ledger operation failed: {exc}") from exc

    def _execute_write(self, fn):
        try:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    result = fn(self._conn)
                    self._conn.commit()
                    return result
                except BaseException:
                    self._conn.rollback()
                    raise
        except sqlite3.Error as exc:
            raise RunLedgerUnavailable(f"Run ledger write failed: {exc}") from exc

    def record_run_start(
        self,
        *,
        job_id: str,
        scheduled_for: datetime | str | None = None,
        trigger_type: str = "schedule",
        parent_run_id: str | None = None,
        attempt: int = 1,
        started_at: datetime | str | None = None,
        logical_date: str | None = None,
        scheduler_instance_id: str | None = None,
        reason_code: str | None = None,
        reason_detail: str | None = None,
        upstream_snapshot: Any = None,
        run_id: str | None = None,
    ) -> str:
        """Insert a running run row and return its run id."""
        run_id = run_id or uuid.uuid4().hex
        started_at = started_at or _utc_now()
        scheduled_for_iso = _iso_utc(scheduled_for)
        logical_date = logical_date or compute_logical_date(scheduled_for)
        upstream_snapshot_json = _json_dumps(upstream_snapshot)

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO runs (
                  run_id, job_id, scheduled_for, logical_date, trigger_type,
                  parent_run_id, attempt, started_at, finished_at, status,
                  reason_code, reason_detail, upstream_snapshot, output_file,
                  dependency_recheck_at, scheduler_instance_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 'running', ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    run_id,
                    job_id,
                    scheduled_for_iso,
                    logical_date,
                    trigger_type,
                    parent_run_id,
                    attempt,
                    _iso_utc(started_at),
                    reason_code,
                    reason_detail,
                    upstream_snapshot_json,
                    scheduler_instance_id,
                ),
            )

        self._execute_write(_insert)
        return run_id

    def record_run_finish(
        self,
        run_id: str,
        *,
        status: str,
        finished_at: datetime | str | None = None,
        reason_code: str | None = None,
        reason_detail: str | None = None,
        upstream_snapshot: Any = None,
        output_file: str | Path | None = None,
        dependency_recheck_at: datetime | str | None = None,
    ) -> None:
        """Mark an existing run row finished."""
        fields: list[str] = [
            "status = ?",
            "finished_at = ?",
        ]
        values: list[Any] = [status, _iso_utc(finished_at or _utc_now())]

        optional_updates = {
            "reason_code": reason_code,
            "reason_detail": reason_detail,
            "upstream_snapshot": _json_dumps(upstream_snapshot) if upstream_snapshot is not None else None,
            "output_file": str(output_file) if output_file is not None else None,
            "dependency_recheck_at": _iso_utc(dependency_recheck_at) if dependency_recheck_at is not None else None,
        }
        for column, value in optional_updates.items():
            if value is not None:
                fields.append(f"{column} = ?")
                values.append(value)
        values.append(run_id)

        def _update(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                f"UPDATE runs SET {', '.join(fields)} WHERE run_id = ?",
                tuple(values),
            )
            if cursor.rowcount == 0:
                raise RunLedgerUnavailable(f"Run id not found: {run_id}")

        self._execute_write(_update)

    def record_gate_blocked(
        self,
        *,
        job_id: str,
        scheduled_for: datetime | str | None = None,
        reason_code: str,
        reason_detail: str | None = None,
        upstream_snapshot: Any = None,
        dependency_recheck_at: datetime | str | None = None,
        output_file: str | Path | None = None,
        trigger_type: str = "schedule",
        parent_run_id: str | None = None,
        attempt: int = 1,
        logical_date: str | None = None,
        scheduler_instance_id: str | None = None,
        recorded_at: datetime | str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Insert a blocked gate-decision row and return its run id."""
        run_id = run_id or uuid.uuid4().hex
        recorded_at = recorded_at or _utc_now()
        scheduled_for_iso = _iso_utc(scheduled_for)
        logical_date = logical_date or compute_logical_date(scheduled_for)

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO runs (
                  run_id, job_id, scheduled_for, logical_date, trigger_type,
                  parent_run_id, attempt, started_at, finished_at, status,
                  reason_code, reason_detail, upstream_snapshot, output_file,
                  dependency_recheck_at, scheduler_instance_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'blocked', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    job_id,
                    scheduled_for_iso,
                    logical_date,
                    trigger_type,
                    parent_run_id,
                    attempt,
                    _iso_utc(recorded_at),
                    _iso_utc(recorded_at),
                    reason_code,
                    reason_detail,
                    _json_dumps(upstream_snapshot),
                    str(output_file) if output_file is not None else None,
                    _iso_utc(dependency_recheck_at),
                    scheduler_instance_id,
                ),
            )

        self._execute_write(_insert)
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        cursor = self._execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def get_latest_terminal(self, job_id: str) -> dict[str, Any] | None:
        placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
        cursor = self._execute(
            f"""
            SELECT * FROM runs
            WHERE job_id = ? AND status IN ({placeholders})
            ORDER BY COALESCE(scheduled_for, finished_at, started_at, '') DESC,
                     COALESCE(finished_at, started_at, '') DESC,
                     run_id DESC
            LIMIT 1
            """,
            (job_id, *sorted(TERMINAL_STATUSES)),
        )
        row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def get_pending_rechecks(self, now: datetime | str | None = None) -> list[dict[str, Any]]:
        terminal_statuses = sorted(TERMINAL_STATUSES - {"blocked"})
        terminal_placeholders = ", ".join("?" for _ in terminal_statuses)
        cursor = self._execute(
            f"""
            SELECT * FROM runs AS candidate
            WHERE candidate.status = 'blocked'
              AND candidate.dependency_recheck_at IS NOT NULL
              AND candidate.dependency_recheck_at <= ?
              AND NOT EXISTS (
                SELECT 1
                FROM runs AS newer
                WHERE newer.job_id = candidate.job_id
                  AND newer.status = 'blocked'
                  AND newer.dependency_recheck_at IS NOT NULL
                  AND (
                    newer.dependency_recheck_at > candidate.dependency_recheck_at
                    OR (
                      newer.dependency_recheck_at = candidate.dependency_recheck_at
                      AND newer.run_id > candidate.run_id
                    )
                  )
              )
              AND NOT EXISTS (
                SELECT 1
                FROM runs AS terminal
                WHERE terminal.job_id = candidate.job_id
                  AND terminal.status IN ({terminal_placeholders})
                  AND COALESCE(terminal.scheduled_for, '') = COALESCE(candidate.scheduled_for, '')
                  AND COALESCE(terminal.finished_at, terminal.started_at, '') >= COALESCE(candidate.finished_at, candidate.started_at, '')
              )
            ORDER BY candidate.dependency_recheck_at ASC, candidate.job_id ASC, candidate.run_id ASC
            """,
            (_iso_utc(now or _utc_now()), *terminal_statuses),
        )
        return [self._row_to_dict(row) for row in cursor.fetchall()]

    def get_future_recheck(self, job_id: str, now: datetime | str | None = None) -> dict[str, Any] | None:
        """Return the latest blocked row whose recheck time is still in the future."""
        cursor = self._execute(
            """
            SELECT * FROM runs
            WHERE job_id = ?
              AND status = 'blocked'
              AND dependency_recheck_at IS NOT NULL
              AND dependency_recheck_at > ?
            ORDER BY dependency_recheck_at DESC, run_id DESC
            LIMIT 1
            """,
            (job_id, _iso_utc(now or _utc_now())),
        )
        row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def reconcile_orphans(
        self,
        scheduler_instance_id: str,
        *,
        finished_at: datetime | str | None = None,
    ) -> int:
        """Mark leftover running rows as failed after scheduler restart."""
        finished_iso = _iso_utc(finished_at or _utc_now())
        detail = f"Reconciled by scheduler {scheduler_instance_id} after restart"

        def _update(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """
                UPDATE runs
                SET status = 'failed',
                    finished_at = ?,
                    reason_code = 'scheduler_restart_orphan',
                    reason_detail = ?,
                    scheduler_instance_id = COALESCE(scheduler_instance_id, ?)
                WHERE status = 'running'
                """,
                (finished_iso, detail, scheduler_instance_id),
            )
            return cursor.rowcount

        return self._execute_write(_update)

    def cleanup_retention(
        self,
        *,
        keep_per_job: int = DEFAULT_KEEP_PER_JOB,
        keep_days: int = DEFAULT_KEEP_DAYS,
        now: datetime | str | None = None,
    ) -> int:
        """Delete rows older than keep_days unless among the latest keep_per_job for a job."""
        cutoff = (_as_utc(now) or _utc_now()) - timedelta(days=keep_days)
        cutoff_iso = _iso_utc(cutoff)

        def _cleanup(conn: sqlite3.Connection) -> int:
            jobs = [row["job_id"] for row in conn.execute("SELECT DISTINCT job_id FROM runs")]
            delete_ids: list[str] = []
            for job_id in jobs:
                rows = conn.execute(
                    """
                    SELECT run_id, COALESCE(finished_at, started_at, scheduled_for, '') AS sort_ts
                    FROM runs
                    WHERE job_id = ?
                    ORDER BY sort_ts DESC, run_id DESC
                    """,
                    (job_id,),
                ).fetchall()
                for index, row in enumerate(rows):
                    if index >= keep_per_job and row["sort_ts"] and row["sort_ts"] < cutoff_iso:
                        delete_ids.append(row["run_id"])
            if not delete_ids:
                return 0
            conn.executemany("DELETE FROM runs WHERE run_id = ?", [(run_id,) for run_id in delete_ids])
            return len(delete_ids)

        return self._execute_write(_cleanup)

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["upstream_snapshot"] = _json_loads(data.get("upstream_snapshot"))
        return data
