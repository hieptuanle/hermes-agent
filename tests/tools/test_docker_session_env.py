"""[4handy] The turn's session context (HERMES_SESSION_*) reaches every ``docker exec``.

The local backend bridges the gateway's per-turn ContextVars into each child env
(``local._inject_session_context_env``); the Docker backend did not, so a tool in a
bot's container could not tell who asked or which post called it (the erp CLI
builds its audit reason from these). Pinned here:

* bound vars go on every exec as name-only ``-e`` flags, values only in the docker
  client's env (never in argv, #96268);
* two conversations served at once each get their OWN sender and post;
* vars never bound in the task are not forwarded, even when the gateway's
  ``os.environ`` holds a stale mirror;
* a cron run binds its job id and name (never the prompt), reset on exit;
* the snapshot strip covers the new cron names.
"""

import contextvars
import re
import threading

import pytest

from gateway import session_context
from gateway.session_context import bound_session_env, clear_session_vars, set_session_vars
from tools.environments import docker as docker_env
from tools.environments.base_session_env import _SNAPSHOT_EXCLUDED_ENV_REGEX


def _make_env():
    env = docker_env.DockerEnvironment.__new__(docker_env.DockerEnvironment)
    env._forward_env = []
    env._env = {}
    env._container_id = "test-container"
    env._docker_exe = "/usr/bin/docker"
    env._init_env_args = []
    env._init_env_values = {}
    env._profile_scoped_passthrough = False
    return env


@pytest.fixture(autouse=True)
def _restore_engaged_latch(monkeypatch):
    # set_session_vars latches a process-global flag; keep it local to this file.
    monkeypatch.setattr(session_context, "_session_context_engaged", session_context._session_context_engaged)


@pytest.fixture
def popen_calls(monkeypatch):
    calls = []
    lock = threading.Lock()

    def _fake_popen(cmd, stdin_data=None, **kw):
        with lock:
            calls.append((list(cmd), kw))
        return object()

    monkeypatch.setattr(docker_env, "_popen_bash", _fake_popen)
    return calls


def _session(actor_id, actor_name, message_id, **extra):
    return dict(platform="mattermost", chat_id="chan1", thread_id="root1", user_id=actor_id,
                user_name=actor_name, message_id=message_id, actor_id=actor_id, actor_name=actor_name, **extra)


def _in_session(fn, **session):
    def run():
        tokens = set_session_vars(**session)
        try:
            return fn()
        finally:
            clear_session_vars(tokens)
    return contextvars.copy_context().run(run)


def _flag_names(cmd):
    return [cmd[i + 1] for i, arg in enumerate(cmd[:-1]) if arg == "-e"]


def test_bound_session_vars_ride_name_only_on_every_exec(popen_calls):
    env = _make_env()
    _in_session(lambda: env._run_bash("erp wt get 1"), **_session("uid-dat", "dat.nguyen", "post-123"))
    _in_session(lambda: env._run_bash("true", login=True), **_session("uid-dat", "dat.nguyen", "post-123"))

    for cmd, kw in popen_calls:
        names = _flag_names(cmd)
        assert {"HERMES_SESSION_ACTOR_ID", "HERMES_SESSION_ACTOR_NAME", "HERMES_SESSION_MESSAGE_ID",
                "HERMES_SESSION_PLATFORM"} <= set(names)
        assert all("=" not in name for name in names)
        assert not any(value in arg for value in ("uid-dat", "dat.nguyen", "post-123") for arg in cmd)
        client_env = kw["env"]
        assert client_env["HERMES_SESSION_ACTOR_ID"] == "uid-dat"
        assert client_env["HERMES_SESSION_ACTOR_NAME"] == "dat.nguyen"
        assert client_env["HERMES_SESSION_MESSAGE_ID"] == "post-123"
        assert client_env["HERMES_SESSION_PLATFORM"] == "mattermost"
        # the flags sit before the container id (docker exec [OPTIONS] CONTAINER COMMAND)
        assert cmd.index("test-container") > max(i for i, a in enumerate(cmd) if a == "-e")


def test_two_concurrent_conversations_each_get_their_own_sender(popen_calls, monkeypatch):
    # The gateway's os.environ mirror holds a third, stale sender: it must reach neither exec.
    monkeypatch.setenv("HERMES_SESSION_ACTOR_ID", "uid-stale")
    monkeypatch.setenv("HERMES_SESSION_MESSAGE_ID", "post-stale")
    env = _make_env()
    barrier = threading.Barrier(2)
    errors = []

    def conversation(actor, name, post):
        def body():
            barrier.wait(timeout=5)  # both turns are bound at the same time
            env._run_bash(f"echo {name}")
            barrier.wait(timeout=5)
        try:
            _in_session(body, **_session(actor, name, post))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=conversation, args=a) for a in
               (("uid-a", "anh.a", "post-a"), ("uid-b", "chi.b", "post-b"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors
    assert len(popen_calls) == 2

    by_command = {cmd[-1]: kw["env"] for cmd, kw in popen_calls}
    env_a = next(v for k, v in by_command.items() if "anh.a" in k)
    env_b = next(v for k, v in by_command.items() if "chi.b" in k)
    assert (env_a["HERMES_SESSION_ACTOR_ID"], env_a["HERMES_SESSION_MESSAGE_ID"]) == ("uid-a", "post-a")
    assert (env_b["HERMES_SESSION_ACTOR_ID"], env_b["HERMES_SESSION_MESSAGE_ID"]) == ("uid-b", "post-b")


def test_unbound_task_forwards_no_session_vars_even_with_env_mirror(popen_calls, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ACTOR_ID", "uid-stale")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "mattermost")
    env = _make_env()
    contextvars.copy_context().run(lambda: env._run_bash("true"))

    cmd, kw = popen_calls[0]
    assert not [n for n in _flag_names(cmd) if n.startswith("HERMES_")]
    assert kw.get("env") is None  # nothing to add: the client inherits, and docker gets no -e for them


def test_bound_session_env_never_reads_os_environ(monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_ACTOR_ID", "uid-stale")
    assert contextvars.copy_context().run(bound_session_env) == {}
    values = _in_session(bound_session_env, **_session("uid-a", "anh.a", "post-a"))
    assert values["HERMES_SESSION_ACTOR_ID"] == "uid-a"
    assert values["HERMES_SESSION_CHAT_NAME"] == ""  # bound-and-cleared is passed as ""
    assert "HERMES_SESSION_CRON_JOB_ID" not in values  # never bound outside cron


def test_cron_run_binds_job_id_and_name_not_prompt(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(scheduler, "_resolve_job_workdir", lambda job, job_id: None)
    job = {"id": "job42", "name": "Báo cáo tồn 7h", "prompt": "secret instructions"}

    def run():
        scope = scheduler._CronRunScope(job, "job42", "exec1")
        scope.enter()
        try:
            inside = bound_session_env()
        finally:
            scope.exit()
        return inside, bound_session_env()

    inside, after = contextvars.copy_context().run(run)
    assert inside["HERMES_CRON_SESSION"] == "1"
    assert inside["HERMES_SESSION_CRON_JOB_ID"] == "job42"
    assert inside["HERMES_SESSION_CRON_JOB_NAME"] == "Báo cáo tồn 7h"
    assert "secret instructions" not in inside.values()
    assert "HERMES_SESSION_CRON_JOB_ID" not in after
    assert "HERMES_SESSION_CRON_JOB_NAME" not in after


def test_cron_job_without_name_binds_empty_name(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(scheduler, "_resolve_job_workdir", lambda job, job_id: None)

    def run():
        scope = scheduler._CronRunScope({"id": "j1", "prompt": "do x"}, "j1", None)
        scope.enter()
        try:
            return bound_session_env()
        finally:
            scope.exit()

    inside = contextvars.copy_context().run(run)
    assert inside["HERMES_SESSION_CRON_JOB_ID"] == "j1"
    assert inside["HERMES_SESSION_CRON_JOB_NAME"] == ""


def test_snapshot_strip_covers_cron_job_vars():
    rx = re.compile(_SNAPSHOT_EXCLUDED_ENV_REGEX)
    for name in ("HERMES_SESSION_CRON_JOB_ID", "HERMES_SESSION_CRON_JOB_NAME"):
        assert rx.match(f'declare -x {name}="x"')
        assert name in session_context._VAR_MAP
