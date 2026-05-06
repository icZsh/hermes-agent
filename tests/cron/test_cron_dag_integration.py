"""Scheduler integration tests for cron DAG gating."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from cron.run_ledger import RunLedger


NOW = datetime(2026, 5, 6, 8, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolate_tick_lock(tmp_path):
    lock_dir = tmp_path / "cron-lock"
    lock_dir.mkdir()
    with patch("cron.scheduler._LOCK_DIR", lock_dir), \
         patch("cron.scheduler._LOCK_FILE", lock_dir / ".tick.lock"), \
         patch("cron.scheduler._hermes_now", return_value=NOW):
        yield


def _dependent_job(**overrides):
    job = {
        "id": "down",
        "name": "downstream",
        "prompt": "down",
        "deliver": "local",
        "enabled": True,
        "next_run_at": "2026-05-06T08:00:00Z",
        "depends_on": ["up"],
        "dependency_window_minutes": 60,
        "dependency_recheck_backoff_seconds": 300,
    }
    job.update(overrides)
    return job


def test_blocked_recheck_does_not_advance_or_execute():
    ledger = RunLedger()
    upstream_run = ledger.record_run_start(job_id="up", scheduled_for=NOW, run_id="up-run")
    ledger.record_run_finish(upstream_run, status="failed", finished_at=NOW)
    ledger.close()

    with patch("cron.scheduler.get_due_jobs", return_value=[_dependent_job()]), \
         patch("cron.scheduler.advance_next_run") as advance_mock, \
         patch("cron.scheduler.run_job") as run_mock, \
         patch("cron.scheduler.mark_job_run") as mark_mock:
        from cron.scheduler import tick

        result = tick(verbose=False)

    ledger = RunLedger()
    pending = ledger.get_pending_rechecks(datetime(2026, 5, 6, 8, 5, tzinfo=timezone.utc))
    ledger.close()

    assert result == 0
    advance_mock.assert_not_called()
    run_mock.assert_not_called()
    mark_mock.assert_not_called()
    assert len(pending) == 1
    assert pending[0]["job_id"] == "down"
    assert pending[0]["reason_code"] == "upstream_failed"


def test_ready_dependency_executes_and_records_success():
    ledger = RunLedger()
    upstream_run = ledger.record_run_start(job_id="up", scheduled_for=NOW, run_id="up-run")
    ledger.record_run_finish(upstream_run, status="success", finished_at=NOW)
    ledger.close()

    with patch("cron.scheduler.get_due_jobs", return_value=[_dependent_job()]), \
         patch("cron.scheduler.advance_next_run") as advance_mock, \
         patch("cron.scheduler.run_job", return_value=(True, "# output", "done", None)), \
         patch("cron.scheduler.save_job_output", return_value="/tmp/down.md"), \
         patch("cron.scheduler._deliver_result", return_value=None), \
         patch("cron.scheduler.mark_job_run") as mark_mock:
        from cron.scheduler import tick

        result = tick(verbose=False)

    ledger = RunLedger()
    latest = ledger.get_latest_terminal("down")
    ledger.close()

    assert result == 1
    advance_mock.assert_called_once_with("down")
    mark_mock.assert_called_once()
    assert latest["status"] == "success"
    assert latest["output_file"] == "/tmp/down.md"


def test_same_tick_upstream_defers_downstream_but_runs_upstream():
    upstream_job = {
        "id": "up",
        "name": "upstream",
        "prompt": "up",
        "deliver": "local",
        "enabled": True,
        "next_run_at": "2026-05-06T08:00:00Z",
        "depends_on": [],
    }

    with patch("cron.scheduler.get_due_jobs", return_value=[upstream_job, _dependent_job()]), \
         patch("cron.scheduler.advance_next_run") as advance_mock, \
         patch("cron.scheduler.run_job", return_value=(True, "# output", "done", None)) as run_mock, \
         patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
         patch("cron.scheduler._deliver_result", return_value=None), \
         patch("cron.scheduler.mark_job_run"):
        from cron.scheduler import tick

        result = tick(verbose=False)

    ledger = RunLedger()
    pending = ledger.get_pending_rechecks(datetime(2026, 5, 6, 8, 5, tzinfo=timezone.utc))
    ledger.close()

    assert result == 1
    run_mock.assert_called_once()
    advance_mock.assert_called_once_with("up")
    assert pending[0]["job_id"] == "down"
    assert pending[0]["reason_code"] == "upstream_due_same_tick"


def test_terminal_block_advances_and_writes_visible_output():
    ledger = RunLedger()
    upstream_run = ledger.record_run_start(job_id="up", scheduled_for=NOW, run_id="up-run")
    ledger.record_run_finish(upstream_run, status="failed", finished_at=NOW)
    ledger.close()

    with patch("cron.scheduler.get_due_jobs", return_value=[
        _dependent_job(next_run_at="2026-05-06T06:30:00Z", dependency_window_minutes=60)
    ]), \
         patch("cron.scheduler.advance_next_run") as advance_mock, \
         patch("cron.scheduler.run_job") as run_mock, \
         patch("cron.scheduler.save_job_output", return_value="/tmp/blocked.md") as save_mock, \
         patch("cron.scheduler._deliver_result", return_value=None) as deliver_mock, \
         patch("cron.scheduler.mark_job_run") as mark_mock:
        from cron.scheduler import tick

        result = tick(verbose=False)

    ledger = RunLedger()
    latest = ledger.get_latest_terminal("down")
    ledger.close()

    assert result == 0
    advance_mock.assert_called_once_with("down")
    run_mock.assert_not_called()
    save_mock.assert_called_once()
    deliver_mock.assert_called_once()
    mark_mock.assert_called_once()
    assert latest["status"] == "blocked"
    assert latest["reason_code"] == "upstream_window_exceeded"
    assert latest["output_file"] == "/tmp/blocked.md"
