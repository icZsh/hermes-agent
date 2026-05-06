"""Read-only DAG observability helpers for cron jobs."""

from __future__ import annotations

from typing import Any

from cron.jobs import get_job, list_jobs
from cron.run_ledger import RunLedger, RunLedgerUnavailable


def dag_status() -> dict[str, Any]:
    jobs = list_jobs(include_disabled=True)
    try:
        ledger = RunLedger()
    except RunLedgerUnavailable as exc:
        return {
            "success": False,
            "error": str(exc),
            "status": "ledger_unavailable",
            "nodes": [_job_node(job, latest=None) for job in jobs],
        }

    try:
        nodes = []
        for job in jobs:
            latest = ledger.get_latest_terminal(job["id"])
            nodes.append(_job_node(job, latest=latest))
        return {"success": True, "count": len(nodes), "nodes": nodes}
    finally:
        ledger.close()


def dag_explain(job_id: str) -> dict[str, Any]:
    root_job = get_job(job_id)
    if not root_job:
        return {"success": False, "error": f"Job not found: {job_id}"}

    try:
        ledger = RunLedger()
    except RunLedgerUnavailable as exc:
        return {"success": False, "error": str(exc), "status": "ledger_unavailable"}

    try:
        path: list[str] = []
        snapshots: dict[str, Any] = {}
        seen: set[str] = set()
        current_id = job_id

        while current_id and current_id not in seen:
            seen.add(current_id)
            path.append(current_id)
            latest = ledger.get_latest_terminal(current_id)
            snapshots[current_id] = {
                "job": _minimal_job(get_job(current_id)),
                "latest": latest,
            }
            if not latest or latest.get("status") != "blocked":
                break
            next_id = _first_failed_upstream(latest.get("upstream_snapshot"))
            if not next_id:
                break
            current_id = next_id

        return {
            "success": True,
            "job_id": job_id,
            "path": path,
            "snapshots": snapshots,
        }
    finally:
        ledger.close()


def _job_node(job: dict[str, Any], *, latest: dict[str, Any] | None) -> dict[str, Any]:
    state = "ready"
    if latest:
        state = latest.get("status") or state
        if latest.get("status") == "blocked" and latest.get("dependency_recheck_at"):
            state = "blocked_recheck"
    return {
        "job_id": job["id"],
        "name": job.get("name"),
        "enabled": job.get("enabled", True),
        "state": state,
        "depends_on": [edge["job_id"] for edge in job.get("depends_on", [])],
        "last_run_status": latest.get("status") if latest else None,
        "last_reason_code": latest.get("reason_code") if latest else None,
        "dependency_recheck_at": latest.get("dependency_recheck_at") if latest else None,
        "latest_run_id": latest.get("run_id") if latest else None,
    }


def _minimal_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not job:
        return None
    return {
        "job_id": job.get("id"),
        "name": job.get("name"),
        "depends_on": [edge["job_id"] for edge in job.get("depends_on", [])],
    }


def _first_failed_upstream(snapshot: Any) -> str | None:
    if not isinstance(snapshot, dict):
        return None
    for upstream_id, details in snapshot.items():
        if isinstance(details, dict) and details.get("ready") is False:
            return str(upstream_id)
    return None
