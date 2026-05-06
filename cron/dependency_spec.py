"""Dependency schema helpers for cron DAG scheduling."""

from __future__ import annotations

import copy
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from hermes_time import get_timezone, now as _hermes_now

from cron.dependency_validators import validate_registered_validators

logger = logging.getLogger(__name__)

DEPENDENCY_FIELD_NAMES = frozenset({
    "depends_on",
    "dependency_window_minutes",
    "dependency_recheck_backoff_seconds",
    "block_on_upstream_running",
    "retry_policy",
    "priority",
})
EDGE_ALLOWED_KEYS = frozenset({"job_id", "policy", "severity", "readiness"})
READINESS_ALLOWED_KEYS = frozenset({
    "type",
    "path_template",
    "path_glob_template",
    "latest_glob_template",
    "freshness",
    "freshness_window_days",
    "validators",
})
RETRY_ALLOWED_KEYS = frozenset({"max_retries", "backoff_seconds"})

DEFAULT_DEPENDENCY_WINDOW_MINUTES = 1440
DEFAULT_RECHECK_BACKOFF_SECONDS = 300
DEFAULT_PRIORITY = 100
DEFAULT_RETRY_POLICY = {"max_retries": 0, "backoff_seconds": 0}


def default_dependencies_manifest_path(hermes_home: Path | None = None) -> Path:
    return (hermes_home or get_hermes_home()) / "cron" / "dependencies.yaml"


def _positive_int(value: Any, *, field_name: str, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if parsed < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return parsed


def _string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        raise ValueError(f"{field_name} must be a string or list of strings")
    return [str(item).strip() for item in items if str(item).strip()]


def normalize_readiness(readiness: Any = None) -> dict[str, Any]:
    if readiness is None:
        readiness = {"type": "job_success"}
    if not isinstance(readiness, dict):
        raise ValueError("dependency readiness must be an object")
    unknown = set(readiness) - READINESS_ALLOWED_KEYS
    if unknown:
        raise ValueError(f"Unknown dependency readiness field(s): {', '.join(sorted(unknown))}")

    normalized: dict[str, Any] = {"type": str(readiness.get("type") or "job_success").strip()}
    if normalized["type"] not in {"artifact", "job_success"}:
        raise ValueError("dependency readiness.type must be artifact or job_success")

    for key in ("path_template", "path_glob_template", "latest_glob_template"):
        value = readiness.get(key)
        if value is not None:
            normalized[key] = str(value).strip()

    freshness = str(readiness.get("freshness") or "none").strip()
    if freshness == "any":
        freshness = "none"
    if freshness not in {"none", "same_local_date", "since_window_start"}:
        raise ValueError("dependency readiness.freshness must be none, same_local_date, or since_window_start")
    normalized["freshness"] = freshness

    if "freshness_window_days" in readiness:
        normalized["freshness_window_days"] = _positive_int(
            readiness.get("freshness_window_days"),
            field_name="readiness.freshness_window_days",
            minimum=0,
        )
    elif freshness == "since_window_start":
        normalized["freshness_window_days"] = 7

    validators = _string_list(readiness.get("validators"), field_name="readiness.validators")
    validate_registered_validators(validators)
    normalized["validators"] = validators
    return normalized


def normalize_dependency_edge(edge: Any) -> dict[str, Any]:
    if isinstance(edge, str):
        job_id = edge.strip()
        if not job_id:
            raise ValueError("dependency job_id cannot be empty")
        return {
            "job_id": job_id,
            "policy": "all_success",
            "severity": "hard",
            "readiness": normalize_readiness({"type": "job_success"}),
        }

    if not isinstance(edge, dict):
        raise ValueError("depends_on entries must be job ids or dependency objects")
    unknown = set(edge) - EDGE_ALLOWED_KEYS
    if unknown:
        raise ValueError(f"Unknown dependency edge field(s): {', '.join(sorted(unknown))}")

    job_id = str(edge.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("dependency job_id is required")
    policy = str(edge.get("policy") or "all_success").strip()
    if policy != "all_success":
        raise ValueError("dependency policy must be all_success for MVP")
    severity = str(edge.get("severity") or "hard").strip()
    if severity not in {"hard", "soft"}:
        raise ValueError("dependency severity must be hard or soft")
    return {
        "job_id": job_id,
        "policy": policy,
        "severity": severity,
        "readiness": normalize_readiness(edge.get("readiness")),
    }


def normalize_depends_on(depends_on: Any) -> list[dict[str, Any]]:
    if depends_on in (None, "", False):
        return []
    if not isinstance(depends_on, list):
        raise ValueError("depends_on must be a list")
    return [normalize_dependency_edge(edge) for edge in depends_on]


def normalize_retry_policy(policy: Any) -> dict[str, int]:
    if policy in (None, "", False):
        return dict(DEFAULT_RETRY_POLICY)
    if not isinstance(policy, dict):
        raise ValueError("retry_policy must be an object")
    unknown = set(policy) - RETRY_ALLOWED_KEYS
    if unknown:
        raise ValueError(f"Unknown retry_policy field(s): {', '.join(sorted(unknown))}")
    return {
        "max_retries": _positive_int(policy.get("max_retries", 0), field_name="retry_policy.max_retries", minimum=0),
        "backoff_seconds": _positive_int(policy.get("backoff_seconds", 0), field_name="retry_policy.backoff_seconds", minimum=0),
    }


def normalize_job_dependency_fields(job: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(job)
    normalized["depends_on"] = normalize_depends_on(normalized.get("depends_on"))
    normalized["dependency_window_minutes"] = _positive_int(
        normalized.get("dependency_window_minutes", DEFAULT_DEPENDENCY_WINDOW_MINUTES),
        field_name="dependency_window_minutes",
        minimum=0,
    )
    normalized["dependency_recheck_backoff_seconds"] = _positive_int(
        normalized.get("dependency_recheck_backoff_seconds", DEFAULT_RECHECK_BACKOFF_SECONDS),
        field_name="dependency_recheck_backoff_seconds",
        minimum=1,
    )
    normalized["block_on_upstream_running"] = bool(normalized.get("block_on_upstream_running", True))
    normalized["retry_policy"] = normalize_retry_policy(normalized.get("retry_policy"))
    normalized["priority"] = _positive_int(
        normalized.get("priority", DEFAULT_PRIORITY),
        field_name="priority",
        minimum=0,
    )
    return normalized


def validate_no_dependency_cycles(jobs: list[dict[str, Any]]) -> None:
    normalized_jobs = [normalize_job_dependency_fields(job) for job in jobs]
    job_ids = {str(job.get("id")) for job in normalized_jobs if job.get("id")}
    graph: dict[str, set[str]] = {job_id: set() for job_id in job_ids}
    indegree: dict[str, int] = {job_id: 0 for job_id in job_ids}

    for job in normalized_jobs:
        downstream_id = str(job.get("id") or "")
        if not downstream_id:
            continue
        for edge in job.get("depends_on", []):
            upstream_id = edge["job_id"]
            if upstream_id not in job_ids:
                continue
            if downstream_id not in graph[upstream_id]:
                graph[upstream_id].add(downstream_id)
                indegree[downstream_id] += 1

    ready = [job_id for job_id, count in indegree.items() if count == 0]
    visited = 0
    while ready:
        job_id = ready.pop()
        visited += 1
        for downstream_id in graph[job_id]:
            indegree[downstream_id] -= 1
            if indegree[downstream_id] == 0:
                ready.append(downstream_id)
    if visited != len(job_ids):
        cycle_nodes = sorted(job_id for job_id, count in indegree.items() if count > 0)
        raise ValueError(f"Cron dependency cycle detected: {' -> '.join(cycle_nodes)}")


def _to_local_date(value: datetime | date | None) -> date:
    if value is None:
        value = _hermes_now()
    if isinstance(value, datetime):
        tz = get_timezone()
        if tz is not None:
            return value.astimezone(tz).date()
        return value.astimezone().date()
    return value


def expand_path_template(
    template: str,
    *,
    when: datetime | date | None = None,
    vault_root: Path | str | None = None,
    hermes_home: Path | str | None = None,
) -> str:
    local_date = _to_local_date(when)
    home = Path(hermes_home).expanduser() if hermes_home is not None else get_hermes_home()
    vault = Path(vault_root).expanduser() if vault_root is not None else Path.home() / "Isaac's Vault"
    replacements = {
        "YYYY": f"{local_date.year:04d}",
        "MM": f"{local_date.month:02d}",
        "DD": f"{local_date.day:02d}",
        "YYYY-MM-DD": local_date.isoformat(),
        "date": local_date.isoformat(),
        "vault_root": str(vault),
        "hermes_home": str(home),
    }
    rendered = str(template)
    for key, value in replacements.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def load_legacy_dependency_manifest(path: Path | str | None = None) -> dict[str, Any]:
    manifest_path = Path(path).expanduser() if path is not None else default_dependencies_manifest_path()
    if not manifest_path.exists():
        return {"version": 1, "dependencies": []}
    try:
        import yaml
    except ImportError as exc:
        raise ValueError("PyYAML is required to read dependencies.yaml") from exc
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"dependency manifest must be a mapping: {manifest_path}")
    dependencies = data.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise ValueError("dependency manifest field 'dependencies' must be a list")
    data["dependencies"] = dependencies
    data.setdefault("version", 1)
    return data


def legacy_dependency_to_edge(dep: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    upstream = dep.get("upstream") or {}
    downstream = dep.get("downstream") or {}
    expected = dep.get("expected") or {}
    upstream_id = str(upstream.get("job_id") or "").strip()
    downstream_id = str(downstream.get("job_id") or "").strip()
    if not upstream_id or not downstream_id:
        return None

    expected_type = str(expected.get("type") or "file").strip()
    readiness_type = "job_success" if expected_type == "cron_status" else "artifact"
    readiness: dict[str, Any] = {
        "type": readiness_type,
        "freshness": expected.get("freshness") or "none",
        "validators": expected.get("validators") or [],
    }
    for key in ("path_template", "path_glob_template", "latest_glob_template"):
        if expected.get(key):
            readiness[key] = expected[key]
    if expected.get("window_start_days_ago") is not None:
        readiness["freshness_window_days"] = expected["window_start_days_ago"]

    edge = normalize_dependency_edge({
        "job_id": upstream_id,
        "policy": "all_success",
        "severity": dep.get("severity") or "hard",
        "readiness": readiness,
    })
    return downstream_id, edge


def merge_legacy_dependencies(
    jobs: list[dict[str, Any]],
    *,
    manifest_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Return normalized job copies augmented with v1 dependencies.yaml edges."""
    merged = [normalize_job_dependency_fields(copy.deepcopy(job)) for job in jobs]
    jobs_by_id = {str(job.get("id")): job for job in merged if job.get("id")}
    manifest = load_legacy_dependency_manifest(manifest_path)
    for dep in manifest.get("dependencies", []):
        if not isinstance(dep, dict):
            continue
        converted = legacy_dependency_to_edge(dep)
        if converted is None:
            continue
        downstream_id, edge = converted
        downstream_job = jobs_by_id.get(downstream_id)
        if downstream_job is None:
            continue
        existing_upstreams = {item["job_id"] for item in downstream_job.get("depends_on", [])}
        if edge["job_id"] not in existing_upstreams:
            downstream_job.setdefault("depends_on", []).append(edge)
    return merged


def normalize_jobs_for_access(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        return merge_legacy_dependencies(jobs)
    except Exception as exc:
        logger.warning("Failed to merge legacy cron dependencies: %s", exc)
        return [normalize_job_dependency_fields(copy.deepcopy(job)) for job in jobs]
