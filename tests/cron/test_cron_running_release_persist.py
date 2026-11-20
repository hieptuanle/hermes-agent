"""4handy local patch: the gateway re-persists ``active_agents`` when a cron run leaves the in-flight set.

``GatewayRunner._active_work_count`` counts registered cron runs. The owner-side persists (session drain
done-callback, tracked writes) fire while the run is still registered, and nothing persisted after
``release_running_job``, so ``gateway_state.json`` kept ``active_agents >= 1`` until the next chat turn
boundary (seen on 8 live gateways 2026-11-20).
"""
import asyncio
from concurrent.futures import Future

import pytest

from cron import scheduler as sched
from gateway.run_bootstrap import _install_cron_release_persist


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield
    sched.set_running_release_listener(None)
    with sched._running_lock:
        for key in list(sched._running_job_ids):
            sched._running_job_ids.discard(key)
            for d in (sched._running_since, sched._running_futures, sched._running_registration_owners,
                      sched._running_allowance_s, sched._running_worker_pids):
                d.pop(key, None)


class FakeRunner:
    """The two runner members the fix touches: the live count and its persist."""

    def __init__(self):
        self.persisted: list[int] = []

    def _active_work_count(self) -> int:
        return len(sched.get_running_job_ids())

    def _persist_active_agents(self) -> None:
        self.persisted.append(self._active_work_count())


def _drain(loop):
    loop.run_until_complete(asyncio.sleep(0))


def test_persisted_count_returns_to_zero_after_cron_release():
    loop = asyncio.new_event_loop()
    try:
        runner = FakeRunner()
        _install_cron_release_persist(runner, loop)
        assert sched.try_register_running_job("job-a") is True
        runner._persist_active_agents()  # owner drain done-callback while the run is still registered
        assert runner.persisted == [1]
        sched.release_running_job("job-a")
        _drain(loop)
        assert runner.persisted[-1] == 0, runner.persisted  # was stuck at 1 before the patch
    finally:
        loop.close()


def test_listener_called_once_per_real_release_only():
    calls = []
    sched.set_running_release_listener(lambda: calls.append(1))
    sched.release_running_job("never-registered")
    assert calls == []
    owner = object()
    assert sched.try_register_running_job("job-b", owner=owner) is True
    sched.release_running_job("job-b", owner=object())  # replaced owner: not released
    assert calls == [] and "job-b" in sched.get_running_job_ids()
    sched.release_running_job("job-b", owner=owner)
    assert calls == [1]


def test_listener_failure_never_breaks_release():
    def boom():
        raise RuntimeError("loop closed")

    sched.set_running_release_listener(boom)
    assert sched.try_register_running_job("job-c") is True
    sched.release_running_job("job-c")
    assert "job-c" not in sched.get_running_job_ids()


def test_stale_sweep_release_notifies(monkeypatch):
    monkeypatch.setattr(sched, "_latest_executions_for_releasable_claims", lambda: {})
    monkeypatch.setattr(sched, "_record_stale_release", lambda *a, **k: None)
    calls = []
    sched.set_running_release_listener(lambda: calls.append(1))
    done = Future()
    done.set_result(None)
    assert sched.try_register_running_job("job-d", future=done) is True
    key = sched._inflight_key("job-d")
    with sched._running_lock:
        sched._running_since[key] -= 86400
    assert sched.sweep_stale_inflight([]) == ["job-d"]
    assert calls == [1]


def test_closed_loop_is_ignored():
    loop = asyncio.new_event_loop()
    runner = FakeRunner()
    _install_cron_release_persist(runner, loop)
    loop.close()
    assert sched.try_register_running_job("job-e") is True
    sched.release_running_job("job-e")  # must not raise
    assert runner.persisted == []
