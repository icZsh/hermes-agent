"""Tests for cron dependency schema normalization."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from cron.dependency_spec import (
    expand_path_template,
    merge_legacy_dependencies,
    normalize_dependency_edge,
    normalize_depends_on,
    validate_no_dependency_cycles,
)
from cron.jobs import create_job, get_job, list_jobs, save_jobs, update_job


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def test_simple_depends_on_expands_to_structured_edge():
    assert normalize_depends_on(["upstream-id"]) == [
        {
            "job_id": "upstream-id",
            "policy": "all_success",
            "severity": "hard",
            "readiness": {
                "type": "job_success",
                "freshness": "none",
                "validators": [],
            },
        }
    ]


def test_unknown_validator_is_rejected():
    with pytest.raises(ValueError, match="Unknown cron dependency validator"):
        normalize_dependency_edge({
            "job_id": "upstream-id",
            "readiness": {
                "type": "artifact",
                "validators": ["exists", "not_a_validator"],
            },
        })


def test_unknown_dependency_fields_are_rejected():
    with pytest.raises(ValueError, match="Unknown dependency edge field"):
        normalize_dependency_edge({"job_id": "upstream-id", "unexpected": True})


def test_create_job_stores_normalized_dependency_fields(tmp_cron_dir):
    upstream = create_job(prompt="up", schedule="every 1h", name="up")
    downstream = create_job(
        prompt="down",
        schedule="every 1h",
        name="down",
        depends_on=[upstream["id"]],
        dependency_window_minutes=30,
        dependency_recheck_backoff_seconds=15,
        retry_policy={"max_retries": 2, "backoff_seconds": 60},
        priority=50,
    )

    stored = get_job(downstream["id"])

    assert stored["depends_on"][0]["job_id"] == upstream["id"]
    assert stored["depends_on"][0]["readiness"]["type"] == "job_success"
    assert stored["dependency_window_minutes"] == 30
    assert stored["dependency_recheck_backoff_seconds"] == 15
    assert stored["retry_policy"] == {"max_retries": 2, "backoff_seconds": 60}
    assert stored["priority"] == 50


def test_update_job_rejects_dependency_cycle(tmp_cron_dir):
    job_a = create_job(prompt="a", schedule="every 1h", name="a")
    job_b = create_job(prompt="b", schedule="every 1h", name="b", depends_on=[job_a["id"]])

    with pytest.raises(ValueError, match="dependency cycle"):
        update_job(job_a["id"], {"depends_on": [job_b["id"]]})

    assert get_job(job_a["id"])["depends_on"] == []


def test_validate_no_dependency_cycles_allows_unknown_upstream_ids():
    validate_no_dependency_cycles([
        {"id": "job-a", "depends_on": ["external-upstream"]},
        {"id": "job-b", "depends_on": ["job-a"]},
    ])


def test_path_template_expands_date_and_roots(tmp_path):
    rendered = expand_path_template(
        "{vault_root}/Hermes/{YYYY}/{MM}/{DD}/{YYYY-MM-DD}.md::{hermes_home}",
        when=date(2026, 5, 6),
        vault_root=tmp_path / "vault",
        hermes_home=tmp_path / "hermes",
    )

    assert rendered == f"{tmp_path}/vault/Hermes/2026/05/06/2026-05-06.md::{tmp_path}/hermes"


def test_dependencies_yaml_merges_missing_edges_without_overwriting_jobs_json(tmp_cron_dir):
    jobs = [
        {"id": "up", "name": "up", "prompt": "up", "schedule": {"kind": "interval", "minutes": 60}},
        {"id": "down", "name": "down", "prompt": "down", "schedule": {"kind": "interval", "minutes": 60}},
    ]
    manifest = tmp_cron_dir / "dependencies.yaml"
    manifest.write_text(
        """
version: 1
dependencies:
  - name: up-before-down
    upstream: {job_id: up, name: up}
    downstream: {job_id: down, name: down}
    severity: soft
    expected:
      type: file
      path_template: "{vault_root}/out/{YYYY-MM-DD}.json"
      validators: [exists, json_valid]
      freshness: same_local_date
""".strip()
        + "\n",
        encoding="utf-8",
    )

    merged = merge_legacy_dependencies(jobs, manifest_path=manifest)

    assert merged[1]["depends_on"][0]["job_id"] == "up"
    assert merged[1]["depends_on"][0]["severity"] == "soft"
    assert merged[1]["depends_on"][0]["readiness"]["type"] == "artifact"
    assert merged[1]["depends_on"][0]["readiness"]["path_template"] == "{vault_root}/out/{YYYY-MM-DD}.json"
    assert jobs[1].get("depends_on") is None


def test_list_jobs_merges_dependencies_yaml_from_hermes_home(tmp_cron_dir):
    hermes_home = Path(os.environ["HERMES_HOME"])
    manifest = hermes_home / "cron" / "dependencies.yaml"
    manifest.write_text(
        """
version: 1
dependencies:
  - name: up-before-down
    upstream: {job_id: up, name: up}
    downstream: {job_id: down, name: down}
    severity: hard
    expected:
      type: cron_status
      validators: [upstream_status_ok]
      freshness: since_window_start
      window_start_days_ago: 7
""".strip()
        + "\n",
        encoding="utf-8",
    )
    save_jobs([
        {"id": "up", "name": "up", "prompt": "up", "schedule": {"kind": "interval", "minutes": 60}, "enabled": True},
        {"id": "down", "name": "down", "prompt": "down", "schedule": {"kind": "interval", "minutes": 60}, "enabled": True},
    ])

    down = next(job for job in list_jobs() if job["id"] == "down")

    assert down["depends_on"][0]["job_id"] == "up"
    assert down["depends_on"][0]["readiness"] == {
        "type": "job_success",
        "freshness": "since_window_start",
        "freshness_window_days": 7,
        "validators": ["upstream_status_ok"],
    }
