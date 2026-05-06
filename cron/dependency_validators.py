"""Validator registry for cron DAG readiness checks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

STATUS_OK = {"ok", "success"}
RUN_STATUS_RE = re.compile(r"(?im)^\s*(?:[-*]\s*)?status\s*[:：]\s*([^\n|]+)")


@dataclass(frozen=True)
class ValidatorResult:
    ok: bool
    note: str | None = None


@dataclass(frozen=True)
class ValidatorContext:
    path: Path | None = None
    upstream_job: dict[str, Any] | None = None


def read_markdown_run_status(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = [match.group(1).strip() for match in RUN_STATUS_RE.finditer(text)]
    for value in matches:
        lowered = value.lower()
        if lowered.startswith(("success", "failed", "skipped", "error")):
            return value
    return matches[0] if matches else None


def _validate_exists(ctx: ValidatorContext) -> ValidatorResult:
    exists = bool(ctx.path and ctx.path.exists())
    return ValidatorResult(exists, f"exists={exists}")


def _validate_json_valid(ctx: ValidatorContext) -> ValidatorResult:
    if not ctx.path or not ctx.path.exists():
        return ValidatorResult(False, "json file missing")
    try:
        with ctx.path.open("r", encoding="utf-8") as fh:
            json.load(fh)
    except Exception as exc:
        return ValidatorResult(False, f"json invalid: {type(exc).__name__}: {exc}")
    return ValidatorResult(True, "json valid")


def _validate_markdown_run_status_not_failed(ctx: ValidatorContext) -> ValidatorResult:
    status = read_markdown_run_status(ctx.path)
    if status is None:
        return ValidatorResult(False, "markdown_run_status=None")
    failed = status.lower().startswith(("failed", "error"))
    return ValidatorResult(not failed, f"markdown_run_status={status}")


def _validate_markdown_run_status_success(ctx: ValidatorContext) -> ValidatorResult:
    status = read_markdown_run_status(ctx.path)
    ok = bool(status and status.lower().startswith("success"))
    return ValidatorResult(ok, f"markdown_run_status={status}")


def _validate_upstream_status_ok(ctx: ValidatorContext) -> ValidatorResult:
    last_status = (ctx.upstream_job or {}).get("last_status")
    ok = str(last_status).lower() in STATUS_OK
    return ValidatorResult(ok, f"last_status={last_status}")


def _validate_upstream_status_not_failed(ctx: ValidatorContext) -> ValidatorResult:
    last_status = (ctx.upstream_job or {}).get("last_status")
    failed = str(last_status).lower() in {"error", "failed"}
    return ValidatorResult(not failed, f"last_status={last_status}")


VALIDATORS: dict[str, Callable[[ValidatorContext], ValidatorResult]] = {
    "exists": _validate_exists,
    "json_valid": _validate_json_valid,
    "markdown_run_status_not_failed": _validate_markdown_run_status_not_failed,
    "markdown_run_status_success": _validate_markdown_run_status_success,
    "upstream_status_ok": _validate_upstream_status_ok,
    "upstream_status_not_failed": _validate_upstream_status_not_failed,
}


def validate_registered_validators(names: list[str]) -> None:
    unknown = sorted({name for name in names if name not in VALIDATORS})
    if unknown:
        raise ValueError(f"Unknown cron dependency validator(s): {', '.join(unknown)}")
