"""Pure dependency gate evaluator for cron DAG scheduling."""

from __future__ import annotations

import glob
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from cron.dependency_spec import expand_path_template, normalize_job_dependency_fields
from cron.dependency_validators import VALIDATORS, ValidatorContext
from cron.run_ledger import RunLedgerUnavailable, compute_logical_date
from hermes_time import now as _hermes_now


@dataclass(frozen=True)
class GateResult:
    status: str
    reason_code: str | None = None
    reason_detail: str | None = None
    upstream_snapshot: dict[str, Any] | None = None
    dependency_recheck_at: datetime | None = None

    @property
    def should_advance_next_run(self) -> bool:
        return self.status in {"ready", "ready_degraded", "blocked_terminal"}

    @property
    def should_execute(self) -> bool:
        return self.status in {"ready", "ready_degraded"}


class LedgerLike(Protocol):
    def get_latest_terminal(self, job_id: str) -> dict[str, Any] | None:
        ...


def evaluate_dependencies(
    job: dict[str, Any],
    ledger: LedgerLike,
    current_tick_due_set: set[str] | None = None,
    now: datetime | None = None,
) -> GateResult:
    """Evaluate whether a job can run without mutating scheduler or ledger state."""
    now = now or _hermes_now()
    try:
        normalized_job = normalize_job_dependency_fields(job)
        dependencies = normalized_job.get("depends_on", [])
        if not dependencies:
            return GateResult("ready")

        current_tick_due_set = current_tick_due_set or set()
        snapshots: dict[str, Any] = {}
        failures: list[dict[str, Any]] = []

        for edge in dependencies:
            edge_result = _evaluate_edge(
                edge,
                job=normalized_job,
                ledger=ledger,
                current_tick_due_set=current_tick_due_set,
                now=now,
            )
            snapshots[edge["job_id"]] = edge_result
            if not edge_result["ready"]:
                failures.append(edge_result)

        if not failures:
            return GateResult("ready", upstream_snapshot=snapshots)

        hard_failures = [failure for failure in failures if failure.get("severity") == "hard"]
        if not hard_failures:
            return GateResult(
                "ready_degraded",
                reason_code=failures[0]["reason_code"],
                reason_detail=_summarize_failures(failures),
                upstream_snapshot=snapshots,
            )

        if _dependency_window_exceeded(normalized_job, now):
            return GateResult(
                "blocked_terminal",
                reason_code="upstream_window_exceeded",
                reason_detail=_summarize_failures(hard_failures),
                upstream_snapshot=snapshots,
            )

        return GateResult(
            "blocked_recheck",
            reason_code=hard_failures[0]["reason_code"],
            reason_detail=_summarize_failures(hard_failures),
            upstream_snapshot=snapshots,
            dependency_recheck_at=now + timedelta(
                seconds=normalized_job.get("dependency_recheck_backoff_seconds", 300)
            ),
        )
    except RunLedgerUnavailable as exc:
        if normalize_job_dependency_fields(job).get("depends_on"):
            return GateResult(
                "blocked_recheck",
                reason_code="ledger_unavailable",
                reason_detail=str(exc),
                dependency_recheck_at=(now or _hermes_now()) + timedelta(
                    seconds=job.get("dependency_recheck_backoff_seconds", 300)
                ),
            )
        return GateResult("ready")
    except Exception as exc:
        return GateResult(
            "gate_error",
            reason_code="gate_evaluation_error",
            reason_detail=f"{type(exc).__name__}: {exc}",
            dependency_recheck_at=(now or _hermes_now()) + timedelta(
                seconds=job.get("dependency_recheck_backoff_seconds", 300)
            ),
        )


def _evaluate_edge(
    edge: dict[str, Any],
    *,
    job: dict[str, Any],
    ledger: LedgerLike,
    current_tick_due_set: set[str],
    now: datetime,
) -> dict[str, Any]:
    upstream_id = edge["job_id"]
    if job.get("block_on_upstream_running", True) and upstream_id in current_tick_due_set:
        return _edge_result(
            edge,
            ready=False,
            reason_code="upstream_due_same_tick",
            reason_detail=f"Upstream {upstream_id} is due in the same scheduler tick",
        )

    latest = ledger.get_latest_terminal(upstream_id)
    readiness = edge["readiness"]
    upstream_job = _upstream_job_from_latest(latest)

    if latest is None:
        return _edge_result(
            edge,
            ready=False,
            latest=latest,
            reason_code="upstream_failed",
            reason_detail=f"Upstream {upstream_id} has no terminal run in the ledger",
        )

    if latest.get("status") != "success":
        return _edge_result(
            edge,
            ready=False,
            latest=latest,
            reason_code="upstream_failed",
            reason_detail=f"Upstream {upstream_id} latest status is {latest.get('status')}",
        )

    artifact_path, artifact_pattern = _resolve_artifact_path(readiness, now=now)
    freshness = _evaluate_freshness(
        readiness,
        latest=latest,
        now=now,
        artifact_path=artifact_path,
    )
    if freshness is not None:
        return _edge_result(
            edge,
            ready=False,
            latest=latest,
            artifact_path=artifact_path,
            artifact_pattern=artifact_pattern,
            reason_code=freshness[0],
            reason_detail=freshness[1],
        )

    validator_failures: list[str] = []
    validator_notes: list[str] = []
    for validator_name in readiness.get("validators", []):
        result = VALIDATORS[validator_name](ValidatorContext(path=artifact_path, upstream_job=upstream_job))
        validator_notes.append(f"{validator_name}: {result.note}")
        if not result.ok:
            validator_failures.append(validator_name)

    if validator_failures:
        reason_code = "upstream_artifact_missing" if "exists" in validator_failures else "upstream_failed"
        return _edge_result(
            edge,
            ready=False,
            latest=latest,
            artifact_path=artifact_path,
            artifact_pattern=artifact_pattern,
            reason_code=reason_code,
            reason_detail=f"failed validators: {', '.join(validator_failures)}; {'; '.join(validator_notes)}",
            failed_validators=validator_failures,
        )

    return _edge_result(
        edge,
        ready=True,
        latest=latest,
        artifact_path=artifact_path,
        artifact_pattern=artifact_pattern,
        reason_code=None,
        reason_detail="upstream ready",
    )


def _edge_result(
    edge: dict[str, Any],
    *,
    ready: bool,
    reason_code: str | None,
    reason_detail: str,
    latest: dict[str, Any] | None = None,
    artifact_path: Path | None = None,
    artifact_pattern: str | None = None,
    failed_validators: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ready": ready,
        "severity": edge.get("severity", "hard"),
        "policy": edge.get("policy", "all_success"),
        "readiness": edge.get("readiness"),
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "failed_validators": failed_validators or [],
        "latest": latest,
        "artifact": {
            "path": str(artifact_path) if artifact_path is not None else None,
            "pattern": artifact_pattern,
            "exists": bool(artifact_path and artifact_path.exists()),
        },
    }


def _resolve_artifact_path(readiness: dict[str, Any], *, now: datetime) -> tuple[Path | None, str | None]:
    if readiness.get("type") != "artifact":
        return None, None

    latest_template = readiness.get("latest_glob_template")
    if latest_template:
        pattern = expand_path_template(latest_template, when=now)
        matches = [Path(path) for path in glob.glob(pattern, recursive=True)]
        matches = [path for path in matches if path.is_file()]
        matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return (matches[0] if matches else None), pattern

    template = readiness.get("path_template") or readiness.get("path_glob_template")
    if not template:
        return None, None
    pattern = expand_path_template(template, when=now)
    if any(ch in pattern for ch in "*?[]"):
        matches = [Path(path) for path in glob.glob(pattern, recursive=True)]
        matches = [path for path in matches if path.is_file()]
        matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return (matches[0] if matches else None), pattern
    return Path(pattern), pattern


def _evaluate_freshness(
    readiness: dict[str, Any],
    *,
    latest: dict[str, Any],
    now: datetime,
    artifact_path: Path | None,
) -> tuple[str, str] | None:
    freshness = readiness.get("freshness", "none")
    if freshness == "none":
        return None

    expected_date = date.fromisoformat(compute_logical_date(now))
    latest_logical = _parse_date(latest.get("logical_date"))
    if freshness == "same_local_date":
        artifact_date = _date_from_path(artifact_path)
        if artifact_date is not None and artifact_date != expected_date:
            return (
                "upstream_artifact_stale",
                f"artifact date {artifact_date.isoformat()} != expected {expected_date.isoformat()}",
            )
        if latest_logical is not None and latest_logical != expected_date:
            return (
                "upstream_artifact_stale",
                f"upstream logical_date {latest_logical.isoformat()} != expected {expected_date.isoformat()}",
            )
        return None

    if freshness == "since_window_start":
        days = int(readiness.get("freshness_window_days", 7))
        window_start = expected_date - timedelta(days=days)
        if latest_logical is None:
            return ("upstream_artifact_stale", f"upstream has no logical_date; window_start={window_start.isoformat()}")
        if latest_logical < window_start:
            return (
                "upstream_artifact_stale",
                f"upstream logical_date {latest_logical.isoformat()} < window_start {window_start.isoformat()}",
            )
    return None


def _dependency_window_exceeded(job: dict[str, Any], now: datetime) -> bool:
    scheduled_for = job.get("_dag_scheduled_for") or job.get("scheduled_for") or job.get("next_run_at")
    scheduled_at = _parse_datetime(scheduled_for)
    if scheduled_at is None:
        return False
    if scheduled_at.tzinfo is not None and now.tzinfo is not None:
        elapsed = now - scheduled_at.astimezone(now.tzinfo)
    else:
        elapsed = now.replace(tzinfo=None) - scheduled_at.replace(tzinfo=None)
    return elapsed >= timedelta(minutes=job.get("dependency_window_minutes", 1440))


def _upstream_job_from_latest(latest: dict[str, Any] | None) -> dict[str, Any]:
    if not latest:
        return {}
    status = latest.get("status")
    return {
        "last_status": "ok" if status == "success" else status,
        "last_run_at": latest.get("finished_at") or latest.get("started_at"),
    }


def _summarize_failures(failures: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{failure.get('reason_code')}: {failure.get('reason_detail')}"
        for failure in failures
    )


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _date_from_path(path: Path | None) -> date | None:
    if path is None:
        return None
    parts = path.name.split("-")
    if len(parts) < 3:
        return None
    candidate = "-".join(parts[:3])
    return _parse_date(candidate)
