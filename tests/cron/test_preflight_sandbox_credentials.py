"""4handy local patch (LOCAL_PATCHES.md "Cron preflight trusts sandbox-held credential files").

Under the docker backend a skill's ``required_credential_files`` (google-workspace's ``google_token.json``)
can live in the persistent sandbox home, where the skill reads and refreshes them; ``skill_view`` only
checks the host HERMES_HOME. The cron preflight must not block such a job. Every other backend keeps
upstream's behaviour, and a missing env var still blocks under docker.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cron.jobs as cron_jobs
from cron.scheduler_preflight import _job_terminal_backend, _preflight_check_skills
from tests.cron.test_preflight_config import _job, _run_job_patched

# run_job needs an in-process cron owner; without it run_canonical_job dials a gateway (and may spawn one).
pytestmark = pytest.mark.usefixtures("cron_owner")

CREDS_ONLY = {
    "success": True,
    "content": "# google-workspace\nbody",
    "readiness_status": "setup_needed",
    "setup_needed": True,
    "missing_required_environment_variables": [],
    "missing_credential_files": ["google_token.json", "google_client_secret.json"],
}
CREDS_AND_ENV = dict(CREDS_ONLY, missing_required_environment_variables=["GOOGLE_PROJECT"])


def _check(payload, cfg):
    with patch("tools.skills_tool.skill_view", return_value=json.dumps(payload)), \
            patch("cron.scheduler_prompt._job_skill_names", return_value=["google-workspace"]):
        return _preflight_check_skills({"id": "j1", "skills": ["google-workspace"]}, cfg)


def test_docker_backend_does_not_block_on_credential_files():
    assert _check(CREDS_ONLY, {"terminal": {"backend": "docker"}}) is None


def test_local_backend_still_blocks_on_credential_files():
    reason = _check(CREDS_ONLY, {"terminal": {"backend": "local"}})
    assert reason is not None and "credential file google_token.json" in reason


def test_docker_backend_still_blocks_on_missing_env():
    reason = _check(CREDS_AND_ENV, {"terminal": {"backend": "docker"}})
    assert reason is not None and "GOOGLE_PROJECT" in reason
    assert "google_token.json" not in reason  # the sandbox-held files are not reported as the gap


def test_backend_comes_from_the_jobs_config_before_the_env(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    assert _job_terminal_backend({"terminal": {"backend": "local"}}) == "local"
    assert _job_terminal_backend({"terminal": {"backend": "Docker"}}) == "docker"


def test_backend_falls_back_to_scope_aware_terminal_env():
    with patch("tools.terminal_scope.terminal_env", return_value="docker") as tenv:
        assert _job_terminal_backend({}) == "docker"
    tenv.assert_called_once_with("TERMINAL_ENV", "local")


def test_docker_job_runs_end_to_end(tmp_path):
    """Through run_job: the job's profile resolves to docker, so it is not blocked and the agent is constructed
    (the backend resolver is patched; a real docker config would route the test run to a gateway)."""
    job = _job(skills=["creds-skill"])
    with cron_jobs.use_cron_store(tmp_path):
        cron_jobs.save_jobs([job])
        with patch("cron.scheduler_preflight._job_terminal_backend", return_value="docker"):
            success, _output, _final, error, agent_constructed = _run_job_patched(
                job, tmp_path, skill_view=lambda *a, **k: json.dumps(CREDS_ONLY))
    assert agent_constructed is True, error
    assert success is True
