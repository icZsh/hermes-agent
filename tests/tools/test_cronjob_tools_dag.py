"""Tests for DAG actions in the cronjob tool."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cron.run_ledger import RunLedger
from tools.cronjob_tools import cronjob


NOW = datetime(2026, 5, 6, 8, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def setup_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")


def test_create_and_list_dag_dependency():
    upstream = json.loads(cronjob(action="create", prompt="up", schedule="every 1h", name="up"))
    downstream = json.loads(
        cronjob(
            action="create",
            prompt="down",
            schedule="every 1h",
            name="down",
            depends_on=[upstream["job_id"]],
            dependency_window_minutes=30,
        )
    )

    listing = json.loads(cronjob(action="list"))
    down = next(job for job in listing["jobs"] if job["job_id"] == downstream["job_id"])

    assert down["depends_on"][0]["job_id"] == upstream["job_id"]
    assert down["dependency_window_minutes"] == 30


def test_dag_status_reports_blocked_recheck():
    upstream = json.loads(cronjob(action="create", prompt="up", schedule="every 1h", name="up"))
    downstream = json.loads(
        cronjob(
            action="create",
            prompt="down",
            schedule="every 1h",
            name="down",
            depends_on=[upstream["job_id"]],
        )
    )
    ledger = RunLedger()
    ledger.record_gate_blocked(
        job_id=downstream["job_id"],
        scheduled_for=NOW,
        reason_code="upstream_failed",
        dependency_recheck_at="2026-05-06T08:05:00Z",
        upstream_snapshot={
            upstream["job_id"]: {
                "ready": False,
                "reason_code": "upstream_failed",
                "reason_detail": "failed",
            }
        },
    )
    ledger.close()

    status = json.loads(cronjob(action="dag_status"))
    down = next(node for node in status["nodes"] if node["job_id"] == downstream["job_id"])

    assert status["success"] is True
    assert down["state"] == "blocked_recheck"
    assert down["last_reason_code"] == "upstream_failed"


def test_dag_explain_chains_to_failed_upstream():
    upstream = json.loads(cronjob(action="create", prompt="up", schedule="every 1h", name="up"))
    downstream = json.loads(
        cronjob(
            action="create",
            prompt="down",
            schedule="every 1h",
            name="down",
            depends_on=[upstream["job_id"]],
        )
    )
    ledger = RunLedger()
    run_id = ledger.record_run_start(job_id=upstream["job_id"], scheduled_for=NOW)
    ledger.record_run_finish(run_id, status="failed", reason_code="agent_execution_failed", finished_at=NOW)
    ledger.record_gate_blocked(
        job_id=downstream["job_id"],
        scheduled_for=NOW,
        reason_code="upstream_failed",
        upstream_snapshot={
            upstream["job_id"]: {
                "ready": False,
                "reason_code": "upstream_failed",
                "reason_detail": "failed",
            }
        },
    )
    ledger.close()

    explanation = json.loads(cronjob(action="dag_explain", job_id=downstream["job_id"]))

    assert explanation["success"] is True
    assert explanation["path"] == [downstream["job_id"], upstream["job_id"]]
    assert explanation["snapshots"][upstream["job_id"]]["latest"]["status"] == "failed"
