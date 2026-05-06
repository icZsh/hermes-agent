"""Tests for cron/run_ledger.py."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from zoneinfo import ZoneInfo

from cron.run_ledger import (
    RunLedger,
    RunLedgerUnavailable,
    compute_logical_date,
    default_runs_db_path,
)


def test_default_path_uses_active_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    assert default_runs_db_path() == tmp_path / "cron" / "runs.db"


def test_init_creates_schema_and_indexes(tmp_path):
    ledger = RunLedger(tmp_path / "runs.db")

    conn = sqlite3.connect(str(tmp_path / "runs.db"))
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    ledger.close()

    assert "runs" in tables
    assert {"idx_job_sched", "idx_job_status", "idx_job_logical", "idx_recheck"} <= indexes
    assert journal_mode == "wal"


def test_record_run_start_and_finish_roundtrip(tmp_path):
    ledger = RunLedger(tmp_path / "runs.db")
    scheduled_for = datetime(2026, 5, 6, 8, 0, tzinfo=timezone.utc)

    run_id = ledger.record_run_start(
        job_id="job-a",
        scheduled_for=scheduled_for,
        trigger_type="schedule",
        scheduler_instance_id="scheduler-1",
        upstream_snapshot={"upstream": {"status": "success"}},
        run_id="run-a",
    )
    ledger.record_run_finish(
        run_id,
        status="success",
        output_file="/tmp/output.md",
        finished_at=scheduled_for + timedelta(minutes=2),
    )

    row = ledger.get_run(run_id)
    ledger.close()

    assert run_id == "run-a"
    assert row["job_id"] == "job-a"
    assert row["scheduled_for"] == "2026-05-06T08:00:00Z"
    assert row["logical_date"] == "2026-05-06"
    assert row["status"] == "success"
    assert row["output_file"] == "/tmp/output.md"
    assert row["upstream_snapshot"] == {"upstream": {"status": "success"}}


def test_record_gate_blocked_and_pending_rechecks(tmp_path):
    ledger = RunLedger(tmp_path / "runs.db")
    now = datetime(2026, 5, 6, 8, 0, tzinfo=timezone.utc)

    due_id = ledger.record_gate_blocked(
        job_id="job-due",
        scheduled_for=now,
        reason_code="upstream_due_same_tick",
        reason_detail="upstream is due in the same tick",
        dependency_recheck_at=now + timedelta(seconds=60),
        upstream_snapshot={"upstream": {"status": "running"}},
        run_id="blocked-due",
    )
    ledger.record_gate_blocked(
        job_id="job-later",
        scheduled_for=now,
        reason_code="upstream_failed",
        dependency_recheck_at=now + timedelta(minutes=10),
        run_id="blocked-later",
    )
    ledger.record_gate_blocked(
        job_id="job-terminal",
        scheduled_for=now,
        reason_code="upstream_window_exceeded",
        output_file="/tmp/blocked.md",
        run_id="blocked-terminal",
    )

    pending = ledger.get_pending_rechecks(now + timedelta(seconds=61))
    row = ledger.get_run(due_id)
    terminal = ledger.get_run("blocked-terminal")
    ledger.close()

    assert [item["run_id"] for item in pending] == ["blocked-due"]
    assert row["status"] == "blocked"
    assert row["reason_code"] == "upstream_due_same_tick"
    assert row["upstream_snapshot"] == {"upstream": {"status": "running"}}
    assert terminal["dependency_recheck_at"] is None
    assert terminal["output_file"] == "/tmp/blocked.md"


def test_pending_rechecks_skip_superseded_rows(tmp_path):
    ledger = RunLedger(tmp_path / "runs.db")
    now = datetime(2026, 5, 6, 8, 0, tzinfo=timezone.utc)

    ledger.record_gate_blocked(
        job_id="job-a",
        scheduled_for=now,
        reason_code="upstream_failed",
        dependency_recheck_at=now - timedelta(minutes=10),
        run_id="job-a-old",
    )
    ledger.record_gate_blocked(
        job_id="job-a",
        scheduled_for=now,
        reason_code="upstream_failed",
        dependency_recheck_at=now - timedelta(minutes=5),
        run_id="job-a-latest-due",
    )
    ledger.record_gate_blocked(
        job_id="job-b",
        scheduled_for=now,
        reason_code="upstream_failed",
        dependency_recheck_at=now - timedelta(minutes=20),
        run_id="job-b-old",
    )
    ledger.record_gate_blocked(
        job_id="job-b",
        scheduled_for=now,
        reason_code="upstream_failed",
        dependency_recheck_at=now + timedelta(minutes=5),
        run_id="job-b-future",
    )

    pending = ledger.get_pending_rechecks(now)
    ledger.close()

    assert [item["run_id"] for item in pending] == ["job-a-latest-due"]


def test_pending_rechecks_skip_rows_resolved_by_later_run(tmp_path):
    ledger = RunLedger(tmp_path / "runs.db")
    scheduled_for = datetime(2026, 5, 6, 8, 0, tzinfo=timezone.utc)

    ledger.record_gate_blocked(
        job_id="job-a",
        scheduled_for=scheduled_for,
        reason_code="upstream_failed",
        dependency_recheck_at=scheduled_for + timedelta(minutes=5),
        recorded_at=scheduled_for,
        run_id="blocked-old",
    )
    run_id = ledger.record_run_start(
        job_id="job-a",
        scheduled_for=scheduled_for,
        run_id="aaa-success-run",
        started_at=scheduled_for + timedelta(minutes=6),
    )
    ledger.record_run_finish(
        run_id,
        status="success",
        finished_at=scheduled_for + timedelta(minutes=7),
    )

    pending = ledger.get_pending_rechecks(scheduled_for + timedelta(minutes=10))
    latest = ledger.get_latest_terminal("job-a")
    ledger.close()

    assert pending == []
    assert latest["run_id"] == "aaa-success-run"


def test_get_latest_terminal_breaks_same_schedule_ties_by_finish_time(tmp_path):
    ledger = RunLedger(tmp_path / "runs.db")
    scheduled_for = datetime(2026, 5, 6, 8, 0, tzinfo=timezone.utc)

    ledger.record_gate_blocked(
        job_id="job-a",
        scheduled_for=scheduled_for,
        reason_code="upstream_failed",
        recorded_at=scheduled_for,
        run_id="zzz-blocked",
    )
    run_id = ledger.record_run_start(
        job_id="job-a",
        scheduled_for=scheduled_for,
        started_at=scheduled_for + timedelta(minutes=1),
        run_id="aaa-success",
    )
    ledger.record_run_finish(run_id, status="success", finished_at=scheduled_for + timedelta(minutes=2))

    latest = ledger.get_latest_terminal("job-a")
    ledger.close()

    assert latest["run_id"] == "aaa-success"


def test_get_latest_terminal_ignores_running_rows(tmp_path):
    ledger = RunLedger(tmp_path / "runs.db")

    ledger.record_gate_blocked(
        job_id="job-a",
        scheduled_for="2026-05-06T08:00:00Z",
        reason_code="upstream_failed",
        run_id="old-blocked",
    )
    ledger.record_run_start(
        job_id="job-a",
        scheduled_for="2026-05-06T09:00:00Z",
        run_id="running",
    )
    latest_success = ledger.record_run_start(
        job_id="job-a",
        scheduled_for="2026-05-06T10:00:00Z",
        run_id="latest-success",
    )
    ledger.record_run_finish(latest_success, status="success", finished_at="2026-05-06T10:05:00Z")

    latest = ledger.get_latest_terminal("job-a")
    ledger.close()

    assert latest["run_id"] == "latest-success"


def test_reconcile_orphans_marks_running_failed(tmp_path):
    ledger = RunLedger(tmp_path / "runs.db")
    ledger.record_run_start(job_id="job-a", run_id="run-a")
    ledger.record_run_start(job_id="job-b", run_id="run-b")
    ledger.record_gate_blocked(job_id="job-c", reason_code="upstream_failed", run_id="blocked")

    count = ledger.reconcile_orphans(
        "scheduler-new",
        finished_at=datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc),
    )

    row_a = ledger.get_run("run-a")
    row_b = ledger.get_run("run-b")
    blocked = ledger.get_run("blocked")
    ledger.close()

    assert count == 2
    assert row_a["status"] == "failed"
    assert row_b["reason_code"] == "scheduler_restart_orphan"
    assert row_b["finished_at"] == "2026-05-06T09:00:00Z"
    assert blocked["status"] == "blocked"


def test_cleanup_retention_keeps_recent_and_minimum_per_job(tmp_path):
    ledger = RunLedger(tmp_path / "runs.db")
    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)

    for index in range(5):
        run_id = ledger.record_run_start(
            job_id="job-a",
            scheduled_for=now - timedelta(days=120 + index),
            run_id=f"old-{index}",
        )
        ledger.record_run_finish(
            run_id,
            status="success",
            finished_at=now - timedelta(days=120 + index),
        )
    recent = ledger.record_run_start(
        job_id="job-a",
        scheduled_for=now - timedelta(days=2),
        run_id="recent",
    )
    ledger.record_run_finish(recent, status="success", finished_at=now - timedelta(days=2))

    deleted = ledger.cleanup_retention(keep_per_job=3, keep_days=90, now=now)

    remaining = {run_id for (run_id,) in sqlite3.connect(str(tmp_path / "runs.db")).execute("SELECT run_id FROM runs")}
    ledger.close()

    assert deleted == 3
    assert remaining == {"recent", "old-0", "old-1"}


def test_unavailable_when_db_path_is_directory(tmp_path):
    with pytest.raises(RunLedgerUnavailable):
        RunLedger(tmp_path)


def test_operation_error_raises_unavailable(tmp_path):
    ledger = RunLedger(tmp_path / "runs.db")
    ledger.close()

    with pytest.raises(RunLedgerUnavailable):
        ledger.record_run_start(job_id="job-a")


def test_compute_logical_date_respects_configured_timezone(monkeypatch):
    monkeypatch.setattr("cron.run_ledger.get_timezone", lambda: ZoneInfo("America/Los_Angeles"))

    assert compute_logical_date("2026-05-06T06:30:00Z") == "2026-05-05"
