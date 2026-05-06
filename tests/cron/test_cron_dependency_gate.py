"""Tests for cron dependency gate evaluation."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from cron.dependency_gate import evaluate_dependencies
from cron.run_ledger import RunLedgerUnavailable


NOW = datetime(2026, 5, 6, 8, 0, tzinfo=timezone.utc)


class FakeLedger:
    def __init__(self, latest_by_job=None, error: Exception | None = None):
        self.latest_by_job = latest_by_job or {}
        self.error = error

    def get_latest_terminal(self, job_id):
        if self.error:
            raise self.error
        return self.latest_by_job.get(job_id)


def success_run(job_id="up", logical_date="2026-05-06"):
    return {
        "run_id": f"{job_id}-run",
        "job_id": job_id,
        "status": "success",
        "logical_date": logical_date,
        "finished_at": f"{logical_date}T08:00:00Z",
    }


def base_job(**overrides):
    job = {
        "id": "down",
        "next_run_at": "2026-05-06T08:00:00Z",
        "depends_on": ["up"],
        "dependency_window_minutes": 60,
        "dependency_recheck_backoff_seconds": 300,
    }
    job.update(overrides)
    return job


def test_no_dependencies_ready_without_reading_unavailable_ledger():
    result = evaluate_dependencies(
        {"id": "solo", "depends_on": []},
        FakeLedger(error=RunLedgerUnavailable("no db")),
        now=NOW,
    )

    assert result.status == "ready"
    assert result.should_execute is True
    assert result.should_advance_next_run is True


def test_job_success_dependency_ready():
    result = evaluate_dependencies(base_job(), FakeLedger({"up": success_run()}), now=NOW)

    assert result.status == "ready"
    assert result.should_execute is True
    assert result.upstream_snapshot["up"]["ready"] is True


def test_hard_failed_upstream_blocks_for_recheck_inside_window():
    result = evaluate_dependencies(
        base_job(),
        FakeLedger({"up": {"job_id": "up", "status": "failed", "logical_date": "2026-05-06"}}),
        now=NOW,
    )

    assert result.status == "blocked_recheck"
    assert result.reason_code == "upstream_failed"
    assert result.should_advance_next_run is False
    assert result.dependency_recheck_at == NOW + timedelta(seconds=300)


def test_hard_failure_after_window_becomes_terminal_block():
    result = evaluate_dependencies(
        base_job(next_run_at="2026-05-06T06:59:00Z"),
        FakeLedger({"up": {"job_id": "up", "status": "failed", "logical_date": "2026-05-06"}}),
        now=NOW,
    )

    assert result.status == "blocked_terminal"
    assert result.reason_code == "upstream_window_exceeded"
    assert result.should_advance_next_run is True
    assert result.should_execute is False


def test_dependency_window_uses_original_dag_scheduled_for_after_fast_forward():
    result = evaluate_dependencies(
        base_job(
            next_run_at="2026-05-06T09:00:00Z",
            _dag_scheduled_for="2026-05-06T06:59:00Z",
        ),
        FakeLedger({"up": {"job_id": "up", "status": "failed", "logical_date": "2026-05-06"}}),
        now=NOW,
    )

    assert result.status == "blocked_terminal"
    assert result.reason_code == "upstream_window_exceeded"


def test_soft_failed_upstream_degrades_but_runs():
    result = evaluate_dependencies(
        base_job(depends_on=[{"job_id": "up", "severity": "soft"}]),
        FakeLedger({"up": {"job_id": "up", "status": "failed", "logical_date": "2026-05-06"}}),
        now=NOW,
    )

    assert result.status == "ready_degraded"
    assert result.reason_code == "upstream_failed"
    assert result.should_execute is True
    assert result.should_advance_next_run is True


def test_same_tick_upstream_defers_downstream():
    result = evaluate_dependencies(
        base_job(),
        FakeLedger({"up": success_run()}),
        current_tick_due_set={"up", "down"},
        now=NOW,
    )

    assert result.status == "blocked_recheck"
    assert result.reason_code == "upstream_due_same_tick"
    assert result.dependency_recheck_at == NOW + timedelta(seconds=300)


def test_artifact_dependency_ready_with_valid_json(tmp_path):
    artifact = tmp_path / "2026-05-06-report.json"
    artifact.write_text('{"ok": true}', encoding="utf-8")
    job = base_job(depends_on=[{
        "job_id": "up",
        "readiness": {
            "type": "artifact",
            "path_template": str(artifact),
            "freshness": "same_local_date",
            "validators": ["exists", "json_valid", "upstream_status_ok"],
        },
    }])

    result = evaluate_dependencies(job, FakeLedger({"up": success_run()}), now=NOW)

    assert result.status == "ready"
    assert result.upstream_snapshot["up"]["artifact"]["path"] == str(artifact)


def test_artifact_missing_blocks_with_specific_reason(tmp_path):
    job = base_job(depends_on=[{
        "job_id": "up",
        "readiness": {
            "type": "artifact",
            "path_template": str(tmp_path / "missing.json"),
            "validators": ["exists", "json_valid"],
        },
    }])

    result = evaluate_dependencies(job, FakeLedger({"up": success_run()}), now=NOW)

    assert result.status == "blocked_recheck"
    assert result.reason_code == "upstream_artifact_missing"
    assert result.upstream_snapshot["up"]["failed_validators"] == ["exists", "json_valid"]


def test_latest_glob_template_resolves_newest_artifact(tmp_path):
    older = tmp_path / "2026-05-05-report.json"
    newer = tmp_path / "2026-05-06-report.json"
    older.write_text('{"ok": false}', encoding="utf-8")
    newer.write_text('{"ok": true}', encoding="utf-8")
    os.utime(older, (NOW.timestamp() - 60, NOW.timestamp() - 60))
    os.utime(newer, (NOW.timestamp(), NOW.timestamp()))

    job = base_job(depends_on=[{
        "job_id": "up",
        "readiness": {
            "type": "artifact",
            "latest_glob_template": str(tmp_path / "*-report.json"),
            "validators": ["exists", "json_valid"],
        },
    }])

    result = evaluate_dependencies(job, FakeLedger({"up": success_run()}), now=NOW)

    assert result.status == "ready"
    assert result.upstream_snapshot["up"]["artifact"]["path"] == str(newer)


def test_same_local_date_freshness_blocks_stale_artifact(tmp_path):
    artifact = tmp_path / "2026-05-05-report.md"
    artifact.write_text("- status: success\n", encoding="utf-8")
    job = base_job(depends_on=[{
        "job_id": "up",
        "readiness": {
            "type": "artifact",
            "path_template": str(artifact),
            "freshness": "same_local_date",
            "validators": ["exists", "markdown_run_status_success"],
        },
    }])

    result = evaluate_dependencies(job, FakeLedger({"up": success_run(logical_date="2026-05-05")}), now=NOW)

    assert result.status == "blocked_recheck"
    assert result.reason_code == "upstream_artifact_stale"


def test_since_window_start_freshness_accepts_recent_upstream():
    job = base_job(depends_on=[{
        "job_id": "up",
        "readiness": {
            "type": "job_success",
            "freshness": "since_window_start",
            "freshness_window_days": 7,
        },
    }])

    result = evaluate_dependencies(job, FakeLedger({"up": success_run(logical_date="2026-05-01")}), now=NOW)

    assert result.status == "ready"


def test_ledger_unavailable_fail_closed_for_dependency_job():
    result = evaluate_dependencies(
        base_job(),
        FakeLedger(error=RunLedgerUnavailable("database is locked")),
        now=NOW,
    )

    assert result.status == "blocked_recheck"
    assert result.reason_code == "ledger_unavailable"
