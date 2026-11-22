"""4handy ECOM-LOST (2026-11-22): the idle reaper dropped a profile-scoped env while a background
process it hosted was still running.

Persistent Docker caches the env under the collapsed container key ``profile:<name>`` (or ``default``),
and a background session records that key as ``task_id`` and the raw spawning task (a cron or chat
task id) as ``owner_task_id``. ``has_active_processes`` matches the owner only (#120546), and the
container-key keep-alive applied to ``<base>:ws:<digest>`` keys only. So 300 s after the last
foreground call the reaper dropped ``profile:<name>``, the poller's next ``execute`` raised, and the
still-running process was reported ``lost`` / ``backend_lost``.
"""

from __future__ import annotations

import time

import pytest

from tools import process_registry as pr_module
from tools import terminal_tool as tt
from tools.process_registry import ProcessRegistry, ProcessSession
from tools.terminal_tool_lifecycle import _cleanup_inactive_envs


class _Env:
    def __init__(self):
        self.cleaned = False

    def cleanup(self):
        self.cleaned = True

    def stop_idle(self):
        self.cleaned = True


@pytest.fixture()
def reg(monkeypatch):
    monkeypatch.setattr(tt, "_active_environments", {})
    monkeypatch.setattr(tt, "_last_activity", {})
    monkeypatch.setattr(tt, "_creation_locks", {})
    registry = ProcessRegistry()
    monkeypatch.setattr(pr_module, "process_registry", registry)
    return registry


def _idle_env(key: str) -> _Env:
    env = _Env()
    tt._active_environments[key] = env
    tt._last_activity[key] = time.time() - 3600
    return env


def _bg(registry: ProcessRegistry, *, container: str, owner: str, sid: str = "proc_bg1",
        exited: bool = False) -> ProcessSession:
    session = ProcessSession(id=sid, command="python run.py --execute", task_id=container,
                             owner_task_id=owner, started_at=time.time(), exited=exited)
    registry._running[sid] = session
    return session


@pytest.mark.parametrize("key", ["profile:hungnguyenbot", "default"])
def test_a_running_background_process_keeps_its_container_key_alive(reg, key):
    # The cron run's raw task id is the owner; the env is cached under the container key.
    env = _idle_env(key)
    _bg(reg, container=key, owner="cron_74b07f132111_20261122_0900")

    _cleanup_inactive_envs(lifetime_seconds=300)

    assert env.cleaned is False
    assert tt._active_environments[key] is env
    assert time.time() - tt._last_activity[key] < 5


def test_the_env_is_reaped_once_its_background_process_exited(reg):
    env = _idle_env("profile:hungnguyenbot")
    _bg(reg, container="profile:hungnguyenbot", owner="cron_x", exited=True)

    _cleanup_inactive_envs(lifetime_seconds=300)

    assert env.cleaned is True
    assert "profile:hungnguyenbot" not in tt._active_environments


def test_a_process_in_another_container_does_not_keep_this_one_alive(reg):
    env = _idle_env("profile:hungnguyenbot")
    other = _idle_env("profile:khangbot")
    _bg(reg, container="profile:khangbot", owner="cron_y")

    _cleanup_inactive_envs(lifetime_seconds=300)

    assert env.cleaned is True
    assert other.cleaned is False


def test_owner_keyed_env_still_kept_alive(reg):
    # Unchanged path: per-session isolation keys the env by the raw task id itself.
    env = _idle_env("session-42")
    _bg(reg, container="session-42", owner="session-42")

    _cleanup_inactive_envs(lifetime_seconds=300)

    assert env.cleaned is False


def test_workspace_key_still_kept_alive(reg):
    env = _idle_env("profile:beta:ws:abcd")
    _bg(reg, container="profile:beta:ws:abcd", owner="local-a")

    _cleanup_inactive_envs(lifetime_seconds=300)

    assert env.cleaned is False
